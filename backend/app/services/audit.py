"""金额审计：关账留痕、拒因留痕、全口径巡检。

所有关账事件都必须在此留痕，且 algorithm 只能是 amount.AMOUNT_ALGORITHM；
出现第二种算法标识或重放值与落账值不符，即为口径混用/长期拧花。
"""
from __future__ import annotations

from app.db import db, rows
from app.services.amount import (
    AMOUNT_ALGORITHM,
    amounts_match,
    replay_settle_amount,
)
from app.services.core import now_str


def record_audit(
    conn,
    *,
    session_id: int,
    event: str,
    shown_amount: float | None,
    replay_amount: float,
    kwh: float,
    price_locked: float,
    result: str,
    note: str = "",
    settled_amount: float | None = None,
) -> None:
    """在调用方事务内追加一条金额审计（与关账同刷、同生共死）。"""
    conn.execute(
        """INSERT INTO amount_audits
           (session_id,event,algorithm,shown_amount,replay_amount,settled_amount,
            kwh,price_locked,result,created_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            event,
            AMOUNT_ALGORITHM,
            shown_amount,
            replay_amount,
            settled_amount,
            kwh,
            price_locked,
            result,
            now_str(),
            note,
        ),
    )


def record_rejected_audit(
    *,
    session_id: int,
    event: str,
    shown_amount: float | None,
    replay_amount: float,
    kwh: float,
    price_locked: float,
    note: str,
) -> None:
    """关账被拒时用独立连接留痕——主事务要整体回滚，审计必须留下。"""
    with db() as conn:
        conn.execute(
            """INSERT INTO amount_audits
               (session_id,event,algorithm,shown_amount,replay_amount,settled_amount,
                kwh,price_locked,result,created_at,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                event,
                AMOUNT_ALGORITHM,
                shown_amount,
                replay_amount,
                None,
                kwh,
                price_locked,
                "拒绝",
                now_str(),
                note,
            ),
        )


def list_audits(session_id: int | None = None) -> list[dict]:
    with db() as conn:
        if session_id is None:
            rs = conn.execute(
                "SELECT * FROM amount_audits ORDER BY id DESC LIMIT 300"
            ).fetchall()
        else:
            rs = conn.execute(
                "SELECT * FROM amount_audits WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return rows(rs)


def sweep_amounts(conn) -> list[dict]:
    """全量巡检：每个会话都按唯一算法重放，揪出任何口径分叉。

    - 在充会话：预估列展示值必须等于重放值，否则是旁路改预估。
    - 已关账会话：落账金额、展示金额必须都等于重放值，且审计算法标识唯一；
      任何一项不符即记为拧花，回执/详情/总览长期不一致都由此发现。
    """
    findings: list[dict] = []
    sessions = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
    for se in sessions:
        kwh = float(se["kwh"])
        price = float(se["price_locked"] or 0.0)
        replay = replay_settle_amount(kwh, price)
        shown = float(se["amount"])
        row = {
            "会话编号": se["id"],
            "会话号": se["slip_no"],
            "状态": se["status"],
            "锁定电价": price,
            "累计电量": kwh,
            "预估金额": shown,
            "重放金额": replay,
        }
        if se["status"] == "已关账":
            settled = se["settle_amount"]
            row["关账金额"] = None if settled is None else float(settled)
            if se["amount_algorithm"] != AMOUNT_ALGORITHM:
                findings.append({**row, "问题": f"算法标识混用：{se['amount_algorithm']}"})
                continue
            if settled is None or not amounts_match(float(settled), replay):
                findings.append({**row, "问题": "关账金额与重放不符（落账口径分叉）"})
                continue
            if not amounts_match(shown, replay):
                findings.append({**row, "问题": "已关账展示金额与重放不符（回执/详情拧花）"})
                continue
        elif not amounts_match(shown, replay):
            findings.append({**row, "问题": "预估金额列与重放不符（旁路篡改或异算法）"})
    return findings
