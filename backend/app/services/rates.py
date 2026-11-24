"""费率档服务。

改档价是与关账并列的写口：路由层以 db(immediate=True) 进入即抢写锁，
把改价与关账串行化，杜绝并发半截状态。会话中途改档不回溯——在充会话的
price_locked 是开会话时的冻结值，本写口绝不改写任何会话行；新价只影响
此后新开的会话。
"""
from __future__ import annotations

from fastapi import HTTPException


def update_rate(conn, rate_id: int, peak: float, flat: float, valley: float) -> dict:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_id,)).fetchone()
    if not plan:
        raise HTTPException(404, detail="费率档不存在")
    for name, val in (("峰价", peak), ("平价", flat), ("谷价", valley)):
        if val < 0:
            raise HTTPException(400, detail=f"{name}不可为负")

    conn.execute(
        "UPDATE rate_plans SET peak_price=?, flat_price=?, valley_price=? WHERE id=?",
        (peak, flat, valley, rate_id),
    )
    # 显式不触碰 sessions.price_locked：在充/已关账会话的电价在开会话时冻结。
    frozen = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE rate_plan_id=? AND price_locked IS NOT NULL",
        (rate_id,),
    ).fetchone()["n"]
    return {
        "费率编号": rate_id,
        "名称": plan["name"],
        "峰价": peak,
        "平价": flat,
        "谷价": valley,
        "锁定电价不受影响会话数": frozen,
    }
