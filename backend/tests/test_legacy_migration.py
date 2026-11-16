from pathlib import Path
import os
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.seed import SCHEMA, init_db  # noqa: E402


@pytest.fixture(scope="module")
def legacy_db(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("legacy")
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
    db_path = data_dir / "app.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO shifts(id,name,keeper,status) VALUES "
        "(1,'早班','阿陈','已交班'),(2,'晚班','小林','值班中')"
    )
    # 旧快照回归留下的「出口半截」：完成单已闭合，托管行残留且批次仍挂托管态、归属已提前改到接班方
    conn.execute(
        "INSERT INTO batches(id,biz_key,title,owner_shift_id,status) VALUES "
        "(1,'K1','切片一',2,'托管中'),(2,'K2','切片二',2,'托管中')"
    )
    conn.execute(
        "INSERT INTO handovers(id,from_shift_id,to_shift_id,status,batch_count,started_at,finished_at) "
        "VALUES (1,1,2,'已完成',2,'2026-01-01T00:00:00Z','2026-01-01T00:05:00Z')"
    )
    conn.execute("INSERT INTO handover_items(handover_id,batch_id) VALUES (1,1),(1,2)")
    conn.execute(
        "INSERT INTO escrow_batches(handover_id,batch_id,from_shift_id,escrowed_at) "
        "VALUES (1,1,1,'2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO reconcile_reports(handover_id,exit_type,before_count,escrow_count,"
        "after_from_count,after_to_count,consistent,detail_json,created_at) VALUES "
        "(1,'发起',2,2,0,0,1,'{}','2026-01-01T00:00:00Z'),"
        "(1,'完成交班',2,2,0,2,1,'{}','2026-01-01T00:05:00Z')"
    )
    conn.commit()
    conn.close()

    init_db()  # 启动迁移：修残 + 回填水位
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_legacy_escrow_drift_is_repaired_on_startup(legacy_db):
    # 无托管残留行、无托管态批次：托管已离在途
    assert legacy_db.execute("SELECT COUNT(*) FROM escrow_batches").fetchone()[0] == 0
    assert (
        legacy_db.execute("SELECT COUNT(*) FROM batches WHERE status = '托管中'").fetchone()[0]
        == 0
    )
    # 完成单的清单批次整体回在途并归接班方
    rows = legacy_db.execute("SELECT status, owner_shift_id FROM batches ORDER BY id").fetchall()
    assert all(r["status"] == "在途" and r["owner_shift_id"] == 2 for r in rows)


def test_legacy_watermark_backfilled_from_reconcile(legacy_db):
    wm = legacy_db.execute("SELECT * FROM escrow_watermarks WHERE handover_id = 1").fetchone()
    assert wm is not None
    assert wm["from_before_count"] == 2
    assert wm["escrow_count"] == 2
    assert wm["from_after_start_count"] == 0
    assert wm["exit_status"] == "已完成"
    assert wm["from_after_exit_count"] == 0
    assert wm["to_after_exit_count"] == 2
    assert wm["escrow_after_exit_count"] == 0
