from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

# 已关账会话的金额字段冻结触发器：旁路 UPDATE/INSERT 篡改已关账行金额时
# 直接在 SQLite 层拒绝。唯一合法的关账路径是状态与金额同事务联动写入，
# 触发条件以 status 同时变化为界，允许结算写口，禁止“只改金额展示”。
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_closed_amount_freeze_upd
BEFORE UPDATE OF amount ON sessions
WHEN OLD.status='已关账' AND NEW.status='已关账'
BEGIN
  SELECT RAISE(ABORT, '已关账会话金额冻结，禁止旁路修改（金额审计口径）');
END;
CREATE TRIGGER IF NOT EXISTS trg_closed_settle_freeze_upd
BEFORE UPDATE OF settle_amount ON sessions
WHEN OLD.status='已关账' AND NEW.status='已关账'
BEGIN
  SELECT RAISE(ABORT, '已关账会话落账金额冻结，禁止旁路修改（金额审计口径）');
END;
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  settle_amount REAL,
  price_locked REAL,
  amount_algorithm TEXT NOT NULL DEFAULT 'LOCKED_PRICE_x_KWH_HALF_UP',
  started_at TEXT NOT NULL,
  closed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS amount_audits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  event TEXT NOT NULL,
  algorithm TEXT NOT NULL,
  shown_amount REAL,
  replay_amount REAL NOT NULL,
  settled_amount REAL,
  kwh REAL NOT NULL,
  price_locked REAL NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 旧库迁移：为已存在的 sessions 表补齐关账口径列（新库由 SCHEMA 直接建出）。
MIGRATIONS = (
    "ALTER TABLE sessions ADD COLUMN settle_amount REAL",
    "ALTER TABLE sessions ADD COLUMN amount_algorithm TEXT NOT NULL DEFAULT 'LOCKED_PRICE_x_KWH_HALF_UP'",
)


def connect() -> sqlite3.Connection:
    # isolation_level=None 关闭 Python sqlite3 的隐式事务，改为 autocommit，
    # 事务边界全部由 db()/transaction() 显式管理，避免隐式事务与写口的
    # BEGIN IMMEDIATE 冲突。
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, timeout=10, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 写口串行化时把锁等待收敛为确定行为，配合 BEGIN IMMEDIATE 使用。
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class WriteBusy(Exception):
    """写口抢锁失败：调用方应整笔回滚并以 409 提示重试。"""


# 并发写口抢写锁失败时的统一对外口径：整笔回滚、调用方重试，绝不落半截。
LOCKED_DETAIL = "写口正被另一笔交易占用，本笔已整体回滚，请重试"


@contextmanager
def db(immediate: bool = False):
    """统一事务边界。

    immediate=False：普通请求，开延迟事务，多语句原子提交（开会话、脉冲等）。
    immediate=True ：关账/改档价等写口，进入即抢写锁，把并发写串行化；
                     抢锁失败抛 WriteBusy，由路由层映射 409，杜绝半截与双终态。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise WriteBusy(str(exc))
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    # DDL 走独立 autocommit 连接，不与业务事务混用。
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        for stmt in MIGRATIONS:
            col = stmt.split("ADD COLUMN ", 1)[1].split(" ", 1)[0]
            if col not in existing:
                conn.execute(stmt)
        conn.executescript(TRIGGERS)
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
