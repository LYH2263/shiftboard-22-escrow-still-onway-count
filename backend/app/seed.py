from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  keeper TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '空闲'
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  biz_key TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  owner_shift_id INTEGER REFERENCES shifts(id),
  status TEXT NOT NULL DEFAULT '在途'
    CHECK (status IN ('在途', '托管中', '已结清')),
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS handovers (
  id INTEGER PRIMARY KEY,
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  to_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  status TEXT NOT NULL
    CHECK (status IN ('进行中', '已完成', '已取消', '失败')),
  batch_count INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS handover_items (
  handover_id INTEGER NOT NULL REFERENCES handovers(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id),
  PRIMARY KEY (handover_id, batch_id)
);
-- 托管集合：与在途物理隔离的独立表，每行挂交班单 id。
-- 批次进入此表后既不计入交班方在途，也不计入接班方在途。
CREATE TABLE IF NOT EXISTS escrow_batches (
  handover_id INTEGER NOT NULL REFERENCES handovers(id),
  batch_id INTEGER NOT NULL REFERENCES batches(id) UNIQUE,
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  escrowed_at TEXT NOT NULL,
  PRIMARY KEY (handover_id, batch_id)
);
-- 托管水位证明：每单一行，发起时落库，出口时补齐；只加不改口径。
CREATE TABLE IF NOT EXISTS escrow_watermarks (
  handover_id INTEGER PRIMARY KEY REFERENCES handovers(id),
  from_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  to_shift_id INTEGER NOT NULL REFERENCES shifts(id),
  from_before_count INTEGER NOT NULL,
  to_before_count INTEGER NOT NULL,
  from_after_start_count INTEGER NOT NULL,
  to_after_start_count INTEGER NOT NULL,
  escrow_count INTEGER NOT NULL,
  batch_ids_json TEXT NOT NULL DEFAULT '[]',
  exit_status TEXT NOT NULL DEFAULT '',
  from_after_exit_count INTEGER,
  to_after_exit_count INTEGER,
  escrow_after_exit_count INTEGER,
  consistent INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY,
  handover_id INTEGER REFERENCES handovers(id),
  exit_type TEXT NOT NULL DEFAULT '',
  before_count INTEGER NOT NULL,
  escrow_count INTEGER NOT NULL,
  after_from_count INTEGER NOT NULL,
  after_to_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _migrate(conn) -> None:
    """对旧库补列/补表；全新库由 SCHEMA 直接建齐。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reconcile_reports)")}
    if cols and "exit_type" not in cols:
        conn.execute("ALTER TABLE reconcile_reports ADD COLUMN exit_type TEXT NOT NULL DEFAULT ''")
    if cols and "escrow_count" not in cols:
        conn.execute("ALTER TABLE reconcile_reports ADD COLUMN escrow_count INTEGER NOT NULL DEFAULT 0")
    _repair_escrow_drift(conn)
    _backfill_watermarks(conn)


def _repair_escrow_drift(conn) -> None:
    """修复旧快照回归留下的「出口半截」：清单批次已不在托管表但仍挂托管态、归属被提前改到接班方。

    只动不牵涉任何进行中交班单的孤儿状态：进行中的单由出口校验整单拒绝，绝不在此偷偷放行。
    """
    # 1) 已闭合交班单仍残留托管行：先清行，再让批次出托管归位，避免「托管未离在途」。
    stale = conn.execute(
        """
        SELECT eb.handover_id, eb.batch_id, h.status AS handover_status,
               h.from_shift_id, h.to_shift_id
        FROM escrow_batches eb
        JOIN handovers h ON h.id = eb.handover_id
        WHERE h.status != '进行中'
          AND NOT EXISTS (
            SELECT 1 FROM handover_items hi
            JOIN handovers ah ON ah.id = hi.handover_id
            WHERE hi.batch_id = eb.batch_id AND ah.status = '进行中'
          )
        """
    ).fetchall()
    for row in stale:
        owner = (
            row["to_shift_id"]
            if row["handover_status"] == "已完成"
            else row["from_shift_id"]
        )
        conn.execute(
            "UPDATE batches SET status = '在途', owner_shift_id = ? WHERE id = ?",
            (owner, row["batch_id"]),
        )
        conn.execute(
            "DELETE FROM escrow_batches WHERE handover_id = ? AND batch_id = ?",
            (row["handover_id"], row["batch_id"]),
        )

    # 2) 批次仍挂托管态但托管行已丢：按所属最近一单已闭合交班单的终态归位（完成→接班方，取消/失败→交班方）。
    orphans = conn.execute(
        """
        SELECT b.id AS batch_id, h.status AS handover_status,
               h.from_shift_id, h.to_shift_id
        FROM batches b
        JOIN handover_items hi ON hi.batch_id = b.id
        JOIN handovers h ON h.id = hi.handover_id
        LEFT JOIN escrow_batches eb ON eb.batch_id = b.id
        WHERE b.status = '托管中' AND eb.batch_id IS NULL
          AND h.id = (
            SELECT MAX(lh.id) FROM handover_items lhi
            JOIN handovers lh ON lh.id = lhi.handover_id
            WHERE lhi.batch_id = b.id
          )
          AND NOT EXISTS (
            SELECT 1 FROM handover_items ahi
            JOIN handovers ah ON ah.id = ahi.handover_id
            WHERE ahi.batch_id = b.id AND ah.status = '进行中'
          )
        """
    ).fetchall()
    for row in orphans:
        owner = (
            row["to_shift_id"]
            if row["handover_status"] == "已完成"
            else row["from_shift_id"]
        )
        conn.execute(
            "UPDATE batches SET status = '在途', owner_shift_id = ? WHERE id = ?",
            (owner, row["batch_id"]),
        )
    if stale or orphans:
        conn.commit()


def _backfill_watermarks(conn) -> None:
    """旧库里的交班单没有水位行：用发起报告回填发起段，出口段能补则补。"""
    missing = conn.execute(
        """
        SELECT h.id, h.from_shift_id, h.to_shift_id, h.status, h.started_at, h.finished_at
        FROM handovers h
        WHERE NOT EXISTS (SELECT 1 FROM escrow_watermarks w WHERE w.handover_id = h.id)
        """
    ).fetchall()
    for h in missing:
        start = conn.execute(
            """
            SELECT before_count, escrow_count, after_from_count, after_to_count
            FROM reconcile_reports
            WHERE handover_id = ? AND exit_type = '发起'
            ORDER BY id LIMIT 1
            """,
            (h["id"],),
        ).fetchone()
        exit_rep = conn.execute(
            """
            SELECT exit_type, after_from_count, after_to_count
            FROM reconcile_reports
            WHERE handover_id = ? AND exit_type IN ('完成交班', '取消交班', '交班失败')
            ORDER BY id DESC LIMIT 1
            """,
            (h["id"],),
        ).fetchone()
        batch_ids = [
            r["batch_id"]
            for r in conn.execute(
                "SELECT batch_id FROM handover_items WHERE handover_id = ? ORDER BY batch_id",
                (h["id"],),
            ).fetchall()
        ]
        if start:
            from_before = start["before_count"]
            escrow_count = start["escrow_count"]
            from_after_start = start["after_from_count"]
            to_after_start = start["after_to_count"]
            to_before = to_after_start
        else:
            from_before = escrow_count = from_after_start = to_after_start = to_before = len(batch_ids)
        exit_status = ""
        from_after_exit = to_after_exit = escrow_after_exit = None
        consistent = 1
        if exit_rep:
            exit_status = {
                "完成交班": "已完成",
                "取消交班": "已取消",
                "交班失败": "失败",
            }.get(exit_rep["exit_type"], "")
            from_after_exit = exit_rep["after_from_count"]
            to_after_exit = exit_rep["after_to_count"]
            escrow_after_exit = 0
        conn.execute(
            """
            INSERT INTO escrow_watermarks(
              handover_id, from_shift_id, to_shift_id,
              from_before_count, to_before_count,
              from_after_start_count, to_after_start_count,
              escrow_count, batch_ids_json, exit_status,
              from_after_exit_count, to_after_exit_count, escrow_after_exit_count,
              consistent, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                h["id"], h["from_shift_id"], h["to_shift_id"],
                from_before, to_before, from_after_start, to_after_start,
                escrow_count, json.dumps(batch_ids, ensure_ascii=False), exit_status,
                from_after_exit, to_after_exit, escrow_after_exit,
                consistent, h["started_at"], h["finished_at"],
            ),
        )


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM shifts").fetchone()["c"]
        if n:
            conn.commit()
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for s in data["shifts"]:
            conn.execute(
                "INSERT INTO shifts(id, name, keeper, status) VALUES (?, ?, ?, ?)",
                (s["id"], s["name"], s["keeper"], s["status"]),
            )
        for b in data["batches"]:
            conn.execute(
                "INSERT INTO batches(id, biz_key, title, owner_shift_id, status, note) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    b["id"],
                    b["biz_key"],
                    b["title"],
                    b["owner_shift_id"],
                    b["status"],
                    b.get("note", ""),
                ),
            )
        conn.commit()
