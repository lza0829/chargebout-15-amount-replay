"""HTTP 层夹具：验证结算写口、改档价写口、审计巡检端到端对外口径。"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(db_path):
    from app.main import app

    return TestClient(app)


def _open_pulse(client, kwh=10.0):
    piles = client.get("/api/桩位").json()
    pile = next(p for p in piles if p["status"] == "空闲")
    sid = client.post("/api/开会话", json={"桩位编号": pile["id"]}).json()["会话编号"]
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": kwh, "功率": 7})
    return sid


def test_settle_endpoint_full_consistency(client):
    sid = _open_pulse(client, 10.0)
    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 200
    body = r.json()
    assert body["金额"] == body["关账金额"] == body["重放金额"]
    assert body["算法"] == "LOCKED_PRICE_x_KWH_HALF_UP"

    # 详情、总览、审计端点与回执同口径。
    detail = client.get(f"/api/会话/{sid}").json()
    assert detail["amount"] == body["金额"]
    assert detail["关账金额"] == body["金额"]
    overview = client.get("/api/总览").json()
    assert overview["本日关账金额"] == body["金额"]
    sweep = client.get("/api/金额审计/巡检").json()
    assert sweep["异常数"] == 0
    audits = client.get("/api/金额审计").json()
    assert any(a["session_id"] == sid and a["result"] == "成功" for a in audits)


def test_bypass_estimate_returns_400_not_fake_success(client, db_path):
    sid = _open_pulse(client, 10.0)
    good = client.get(f"/api/会话/{sid}").json()["amount"]
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("UPDATE sessions SET amount=? WHERE id=?", (good + 30, sid))
    finally:
        conn.close()

    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 400
    assert "口径分叉" in r.json()["detail"]
    # 状态未动、无落账金额、拒绝审计已留痕。
    detail = client.get(f"/api/会话/{sid}").json()
    assert detail["status"] == "充电中"
    assert detail["settle_amount"] is None
    audits = client.get("/api/金额审计", params={"会话编号": sid}).json()
    assert any(a["result"] == "拒绝" and a["event"] == "关账拒绝" for a in audits)


def test_rate_change_endpoint_keeps_locked_price(client):
    sid = _open_pulse(client, 5.0)
    before = client.get(f"/api/会话/{sid}").json()["price_locked"]
    r = client.put("/api/费率/1/档价", json={"峰价": 3.3, "平价": 2.2, "谷价": 1.1})
    assert r.status_code == 200
    assert r.json()["锁定电价不受影响会话数"] >= 1
    after = client.get(f"/api/会话/{sid}").json()
    assert after["price_locked"] == before
    # 关账仍按冻结锁定价。
    out = client.post(f"/api/会话/{sid}/结算").json()
    assert out["金额"] == round(before * 5.0, 2)

def test_closed_amount_freeze_via_api(client, db_path):
    sid = _open_pulse(client, 4.0)
    client.post(f"/api/会话/{sid}/结算")
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE sessions SET amount=1 WHERE id=?", (sid,))
    finally:
        conn.close()
    assert client.get("/api/金额审计/巡检").json()["异常数"] == 0


def test_repeat_settle_returns_400(client):
    sid = _open_pulse(client, 2.0)
    assert client.post(f"/api/会话/{sid}/结算").status_code == 200
    r = client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 400
