"""确定性写锁串行化与旧库迁移夹具。"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


def test_settle_returns_409_while_write_lock_held(db_path):
    """另一连接持有写锁时，关账写口必须 409 且库中无半截终态。"""
    from app.db import db
    from app.services import sessions as svc

    with db() as conn:
        row = conn.execute("SELECT id FROM piles WHERE status='空闲' LIMIT 1").fetchone()
        sid = svc.open_session(conn, row["id"])["会话编号"]
        svc.add_pulse(conn, sid, 8.0, 7)

    # 旁路连接长期持有写锁（busy_timeout 缩短，避免测试久等）。
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("PRAGMA busy_timeout=200")
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(Exception) as ei:  # 服务层 WriteBusy
            with db(immediate=True) as conn2:
                svc.settle(conn2, sid)
        from app.db import WriteBusy

        assert isinstance(ei.value, WriteBusy)

        # HTTP 层同样必须是 409。
        from app.main import app

        client = TestClient(app)
        r = client.post(f"/api/会话/{sid}/结算")
        assert r.status_code == 409
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    # 锁释放后会话仍是开放态、无落账金额，可正常关账。
    with db() as conn:
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        assert se["status"] == "充电中"
        assert se["settle_amount"] is None
    with db(immediate=True) as conn:
        out = svc.settle(conn, sid)
    assert out["状态"] == "已关账"


def test_legacy_db_migration_backfills_columns(db_path):
    """模拟旧库（无 settle_amount/amount_algorithm/触发器）迁移后可正常关账。"""
    import os

    from app.db import db
    from app.services import sessions as svc

    # 先按现行结构造一笔真实数据，取其完整字段值。
    with db() as conn:
        row = conn.execute("SELECT id FROM piles WHERE status='空闲' LIMIT 1").fetchone()
        sid = svc.open_session(conn, row["id"])["会话编号"]
        svc.add_pulse(conn, sid, 4.0, 7)
        legacy = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        legacy = {k: legacy[k] for k in legacy.keys()}

    # 删掉重建为“旧版库”：只建旧 schema（缺新列、无触发器、无审计表）。
    os.remove(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.executescript(
            """
            CREATE TABLE rate_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
              peak_price REAL NOT NULL, flat_price REAL NOT NULL,
              valley_price REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE sites (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
              address TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE piles (
              id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER NOT NULL,
              name TEXT NOT NULL, rated_kw REAL NOT NULL DEFAULT 7,
              status TEXT NOT NULL DEFAULT '空闲', rate_plan_id INTEGER NOT NULL);
            CREATE TABLE guns (
              id INTEGER PRIMARY KEY AUTOINCREMENT, pile_id INTEGER NOT NULL,
              name TEXT NOT NULL, status TEXT NOT NULL DEFAULT '可用');
            CREATE TABLE sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, slip_no TEXT NOT NULL UNIQUE,
              site_id INTEGER NOT NULL, pile_id INTEGER NOT NULL,
              gun_id INTEGER NOT NULL, rate_plan_id INTEGER NOT NULL,
              business_day TEXT NOT NULL, status TEXT NOT NULL,
              kwh REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
              price_locked REAL, started_at TEXT NOT NULL,
              closed_at TEXT, note TEXT NOT NULL DEFAULT '');
            CREATE TABLE pulses (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
              delta_kwh REAL NOT NULL, power_kw REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
            CREATE TABLE session_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
              action TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
            CREATE TABLE day_seals (
              id INTEGER PRIMARY KEY AUTOINCREMENT, business_day TEXT NOT NULL UNIQUE,
              sealed INTEGER NOT NULL DEFAULT 0, sealed_at TEXT, note TEXT NOT NULL DEFAULT '');
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute(
            """INSERT INTO sessions
               (id,slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,
                kwh,amount,price_locked,started_at,closed_at,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                legacy["id"], legacy["slip_no"], legacy["site_id"], legacy["pile_id"],
                legacy["gun_id"], legacy["rate_plan_id"], legacy["business_day"],
                legacy["status"], legacy["kwh"], legacy["amount"],
                legacy["price_locked"], legacy["started_at"],
                legacy["closed_at"], legacy["note"],
            ),
        )
    finally:
        conn.close()

    # 重新初始化：CREATE IF NOT EXISTS 不重建 sessions，由迁移补列、补触发器与审计表。
    from app.db import init_db

    init_db()
    with db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert {"settle_amount", "amount_algorithm"} <= cols
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='amount_audits'"
        ).fetchone() is not None

    with db(immediate=True) as conn:
        out = svc.settle(conn, sid)
    assert out["关账金额"] == out["重放金额"]
    # 迁移补建的冻结触发器立即生效。
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE sessions SET amount=1 WHERE id=?", (sid,))
    finally:
        conn.close()
