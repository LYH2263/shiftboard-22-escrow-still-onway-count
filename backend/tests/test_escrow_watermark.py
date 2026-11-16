from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    data_dir = ROOT / "backend" / "data_test_watermark"
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
    db_path = data_dir / "app.db"
    if db_path.exists():
        db_path.unlink()
    with TestClient(app) as c:
        yield c


def _inflight(client, shift_id):
    rows = client.get("/api/batches", params={"owner_shift_id": shift_id}).json()
    return [b for b in rows if b["status"] == "在途"]


def test_watermark_persisted_at_start_and_cited_by_handover_detail(client):
    r = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 2})
    assert r.status_code == 200, r.text
    hid = r.json()["id"]

    r = client.get(f"/api/handovers/{hid}/escrow-watermark")
    assert r.status_code == 200, r.text
    wm = r.json()

    # 发起前 / 发起后两侧在途、托管集合齐全，且与交班单详情同源
    assert wm["handover_status"] == "进行中"
    assert wm["在途水位"]["发起前"] == {"交班方": 4, "接班方": 0}
    assert wm["在途水位"]["发起后"] == {"交班方": 0, "接班方": 0}
    assert wm["在途水位"]["出口后"] is None
    assert wm["托管集合"]["批次数"] == 4
    assert sorted(wm["托管集合"]["批次"]) == sorted(b["batch_id"] for b in wm["托管当前"])
    assert wm["托管集合"]["出口后残留"] is None
    assert wm["两侧在途均不含托管"] is True
    assert wm["consistent"] is True

    detail = client.get(f"/api/handovers/{hid}").json()
    assert detail["托管水位"]["在途水位"] == wm["在途水位"]
    assert detail["托管水位"]["托管集合"] == wm["托管集合"]

    # 托管期间两侧在途都不计清单批次，托管批次归属仍挂交班方
    assert _inflight(client, 1) == []
    assert _inflight(client, 2) == []
    hosted = [b for b in client.get("/api/batches").json() if b["escrow_handover_id"] == hid]
    assert all(b["owner_shift_id"] == 1 for b in hosted)
    assert client.get("/api/dashboard").json()["在途批次数"] == 0


def test_watermark_completed_records_exit_numbers(client):
    hid = client.get("/api/handovers").json()[0]["id"]
    r = client.post(f"/api/handovers/{hid}/complete")
    assert r.status_code == 200, r.text

    wm = client.get(f"/api/handovers/{hid}/escrow-watermark").json()
    assert wm["handover_status"] == "已完成"
    assert wm["exit_status"] == "已完成"
    assert wm["在途水位"]["出口后"] == {"交班方": 0, "接班方": 4}
    assert wm["托管当前"] == []
    assert wm["托管集合"]["出口后残留"] == 0
    assert wm["consistent"] is True
    # 与出口对账报告同源
    rep = next(
        r
        for r in client.get("/api/reconcile").json()
        if r["handover_id"] == hid and r["exit_type"] == "完成交班"
    )
    assert rep["after_from_count"] == wm["在途水位"]["出口后"]["交班方"]
    assert rep["after_to_count"] == wm["在途水位"]["出口后"]["接班方"]


def test_watermark_cancelled_records_exit_numbers(client):
    r = client.post("/api/handovers/start", json={"from_shift_id": 2, "to_shift_id": 3})
    assert r.status_code == 200, r.text
    hid = r.json()["id"]
    r = client.post(f"/api/handovers/{hid}/cancel", json={"reason": "接班方暂不接"})
    assert r.status_code == 200, r.text

    wm = client.get(f"/api/handovers/{hid}/escrow-watermark").json()
    assert wm["exit_status"] == "已取消"
    assert wm["在途水位"]["出口后"] == {"交班方": 4, "接班方": 0}
    assert wm["托管集合"]["出口后残留"] == 0
    assert wm["consistent"] is True


def test_watermark_unknown_handover_404(client):
    assert client.get("/api/handovers/9999/escrow-watermark").status_code == 404


def test_watermark_is_read_only(client):
    hid = client.get("/api/handovers").json()[0]["id"]
    r = client.post(f"/api/handovers/{hid}/escrow-watermark")
    assert r.status_code == 405
