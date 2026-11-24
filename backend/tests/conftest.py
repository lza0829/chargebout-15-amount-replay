"""夹具公共底座：每个用例一个全新临时库，互不污染。

app.config 在首次导入时读取 CHARGEBOUT_DB，因此 conftest 顶部（任何
app.* 导入之前）把默认指到临时目录；每个用例再通过 monkeypatch 改
环境变量并重新执行 init_db/seed，配合全新文件路径实现完全隔离。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

# 任何 app.* 导入之前先给一个隔离路径，避免误触工作区真实库。
os.environ.setdefault("CHARGEBOUT_DB", str(Path(tempfile.gettempdir()) / "chargebout_pytest_base.db"))


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "fixture.db"
    monkeypatch.setenv("CHARGEBOUT_DB", str(path))
    import app.config as config
    import app.db as dbm

    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    dbm.init_db()
    from app.seed import seed_if_empty

    seed_if_empty()
    return path


@pytest.fixture()
def helper(db_path):
    from app.db import db
    from app.services import sessions as svc

    class H:
        def open_with_pulse(self, kwh=10.0, pile_status="空闲"):
            with db() as conn:
                row = conn.execute(
                    "SELECT id FROM piles WHERE status=? LIMIT 1", (pile_status,)
                ).fetchone()
                sid = svc.open_session(conn, row["id"])["会话编号"]
                if kwh:
                    svc.add_pulse(conn, sid, kwh, 7)
                price = conn.execute(
                    "SELECT price_locked FROM sessions WHERE id=?", (sid,)
                ).fetchone()["price_locked"]
            return sid, float(price)

        def settle(self, sid):
            from app.services import sessions as s
            with db(immediate=True) as conn:
                return s.settle(conn, sid)

        def session(self, sid):
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE id=?", (sid,)
                ).fetchone()
                return {k: row[k] for k in row.keys()}

        def audits(self, sid=None):
            from app.services import audit as a
            return a.list_audits(sid)

        def sweep(self):
            from app.services import audit as a
            with db() as conn:
                return a.sweep_amounts(conn)

        def overview_total(self):
            from app.services.core import overview
            with db() as conn:
                return overview(conn)["本日关账金额"]

        def raw_conn(self):
            """旁路连接：绕过所有服务写口，直接写库。"""
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            return conn

    return H()
