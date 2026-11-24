"""关账金额口径闭合夹具。

全局唯一臂：Arm B（关账瞬间按锁定电价×累计电量重算）。六条必逼路径：
  1. 旁路改预估列不得伪装关账成功
  2. 关账瞬间重算与预估列分叉时整笔失败、修回可关
  3. 关账与改档价并发半截必须回滚、无双终态
  4. 回执/详情/总览长期一致（巡检为空）
  5. 总览本日合计与关账回执逐分对得上
  6. 两种算法混用会被审计与巡检拧出来
另含：重放可复算、舍入自洽、已关账金额 DB 冻结、旁路直改拦截。
"""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from app.db import WriteBusy, db
from app.services.amount import AMOUNT_ALGORITHM, replay_settle_amount, round2
from app.services import sessions as svc
from app.services import rates as rsvc


def _bypass_set_amount(db_path, sid, value):
    """模拟旁路：绕过关账写口，直接 UPDATE 预估列。"""
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("UPDATE sessions SET amount=? WHERE id=?", (value, sid))
    finally:
        conn.close()


def _bypass_set_algorithm(db_path, sid, algo):
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute(
            "UPDATE sessions SET amount_algorithm=? WHERE id=?", (algo, sid)
        )
    finally:
        conn.close()


# ---------- 路径1：旁路改预估不得伪装成关账成功 ----------
def test_bypass_estimate_change_rejects_settle(helper, db_path):
    sid, price = helper.open_with_pulse(kwh=10.0)
    expect = replay_settle_amount(10.0, price)
    assert helper.session(sid)["amount"] == expect

    # 旁路把预估列改成与重算口径不符的数字。
    _bypass_set_amount(db_path, sid, expect + 50.0)
    assert helper.session(sid)["amount"] != expect

    with pytest.raises(HTTPException) as ei:
        helper.settle(sid)
    assert ei.value.status_code == 400
    assert "口径分叉" in ei.value.detail

    # 整笔失败：状态仍是开放态，没有任何落账金额。
    se = helper.session(sid)
    assert se["status"] == "充电中"
    assert se["settle_amount"] is None
    # 拒绝事件必须留痕，且算法标识仍是唯一臂。
    audits = helper.audits(sid)
    rej = [a for a in audits if a["result"] == "拒绝"]
    assert rej and rej[-1]["event"] == "关账拒绝"
    assert rej[-1]["algorithm"] == AMOUNT_ALGORITHM
    assert rej[-1]["shown_amount"] == expect + 50.0
    assert rej[-1]["replay_amount"] == expect


# ---------- 路径2：分叉修回同口径后可正常关账，金额由重放决定 ----------
def test_forked_estimate_repairable_then_settle_replays(helper, db_path):
    sid, price = helper.open_with_pulse(kwh=10.0)
    expect = replay_settle_amount(10.0, price)
    _bypass_set_amount(db_path, sid, 1.23)
    with pytest.raises(HTTPException):
        helper.settle(sid)

    # 修回同口径（模拟旁路被纠正），关账成功且落账=重放值，不接受任何展示值。
    _bypass_set_amount(db_path, sid, expect)
    out = helper.settle(sid)
    assert out["关账金额"] == expect
    assert out["重放金额"] == expect
    se = helper.session(sid)
    assert se["status"] == "已关账"
    assert se["settle_amount"] == expect
    assert se["amount"] == expect
    assert se["amount_algorithm"] == AMOUNT_ALGORITHM
    # 巡检必须无异常：预估列、落账、重放三者闭合。
    assert helper.sweep() == []


# ---------- 路径3a：关账与改档价并发，写口串行、半截回滚、锁定价不回溯 ----------
def test_concurrent_settle_vs_rate_change_serialized(helper, db_path):
    sid, locked_price = helper.open_with_pulse(kwh=12.0)
    barrier = threading.Barrier(2)
    results = {}

    def do_settle():
        barrier.wait()
        try:
            results["settle"] = helper.settle(sid)
        except Exception as e:  # noqa: BLE001
            results["settle"] = e

    def do_rate_change():
        barrier.wait()
        try:
            with db(immediate=True) as conn:
                results["rate"] = rsvc.update_rate(conn, 1, 2.5, 2.0, 1.0)
        except Exception as e:  # noqa: BLE001
            results["rate"] = e

    t1 = threading.Thread(target=do_settle)
    t2 = threading.Thread(target=do_rate_change)
    t1.start(); t2.start()
    t1.join(); t2.join()

    se = helper.session(sid)
    # 无论谁先抢到锁：会话只有一个终态，关账金额只由锁定价×电量决定。
    expect = replay_settle_amount(12.0, locked_price)
    if isinstance(results["settle"], dict):
        assert results["settle"]["关账金额"] == expect
        assert se["status"] == "已关账"
        assert se["settle_amount"] == expect
    else:
        # 关账被 409 挡回时，会话必须仍是开放态，绝不出现半截“已关账+空金额”。
        assert isinstance(results["settle"], (WriteBusy, HTTPException))
        assert se["status"] == "充电中"
        assert se["settle_amount"] is None

    # 改档价即使成功，也绝不动在充/已关账会话的锁定电价。
    assert se["price_locked"] == locked_price
    plan = None
    with db() as conn:
        plan = conn.execute("SELECT * FROM rate_plans WHERE id=1").fetchone()
        plan = {k: plan[k] for k in plan.keys()}
    if not isinstance(results["rate"], Exception):
        assert (plan["peak_price"], plan["flat_price"], plan["valley_price"]) == (2.5, 2.0, 1.0)
    # 无论谁先抢到锁，全局口径都闭合：不存在金额与状态分叉。
    assert helper.sweep() == []


# ---------- 路径3b：并发双关账，只有一个终态，失败者 409/400 且无半截 ----------
def test_concurrent_double_settle_single_terminal(helper):
    sid, price = helper.open_with_pulse(kwh=5.5)
    barrier = threading.Barrier(2)
    outcomes = []

    def go():
        barrier.wait()
        try:
            outcomes.append(("ok", helper.settle(sid)))
        except Exception as e:  # noqa: BLE001
            outcomes.append(("err", e))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    oks = [o for o in outcomes if o[0] == "ok"]
    errs = [o for o in outcomes if o[0] == "err"]
    assert len(oks) == 1 and len(errs) == 1
    assert oks[0][1]["关账金额"] == replay_settle_amount(5.5, price)
    assert isinstance(errs[0][1], (WriteBusy, HTTPException))
    se = helper.session(sid)
    assert se["status"] == "已关账"
    assert se["settle_amount"] == replay_settle_amount(5.5, price)
    assert helper.sweep() == []


# ---------- 路径4：回执金额、详情金额长期一致，巡检兜底 ----------
def test_receipt_detail_remain_consistent(helper):
    sids = []
    for kwh in (3.0, 7.25, 11.111):
        sid, price = helper.open_with_pulse(kwh=kwh)
        sids.append((sid, price, kwh))
    receipts = {}
    for sid, _, _ in sids:
        receipts[sid] = helper.settle(sid)

    from app.services.core import session_detail
    with db() as conn:
        for sid, price, kwh in sids:
            replay = replay_settle_amount(kwh, price)
            detail = session_detail(conn, sid)
            # 回执 == 详情 amount == 详情关账金额 == 重放
            assert receipts[sid]["金额"] == replay
            assert detail["amount"] == replay
            assert detail["关账金额"] == replay
            assert detail["金额算法"] == AMOUNT_ALGORITHM
    # 长期拧花巡检：必须为空。
    assert helper.sweep() == []


# ---------- 路径5：总览本日关账合计与回执逐分对得上 ----------
def test_overview_total_matches_receipts(helper):
    total = 0.0
    for kwh in (2.0, 4.5, 8.75):
        sid, price = helper.open_with_pulse(kwh=kwh)
        r = helper.settle(sid)
        total = round2(total + r["关账金额"])
    assert helper.overview_total() == total
    # 与逐笔回执求和完全同源同舍入。
    with db() as conn:
        raw_sum = conn.execute(
            "SELECT COALESCE(SUM(settle_amount),0) s FROM sessions "
            "WHERE status='已关账'"
        ).fetchone()["s"]
    assert round2(raw_sum) == total


# ---------- 路径6：两种算法混用必须被审计/巡检拧出来 ----------
def test_mixed_algorithm_detected_by_audit(helper, db_path):
    sid, price = helper.open_with_pulse(kwh=10.0)
    helper.settle(sid)
    good = replay_settle_amount(10.0, price)

    # 旁路把已关账行伪装成另一种算法标识并塞入异值（绕过冻结触发器的
    # 组合写法：先改算法标识列，再尝试改金额列）。
    _bypass_set_algorithm(db_path, sid, "ARM_A_ESTIMATE_COLUMN_PASSTHROUGH")
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # 金额冻结触发器必须拦住只改金额的旁路（RAISE ABORT 映射为 IntegrityError）。
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE sessions SET amount=? WHERE id=?", (good + 9.99, sid))
        with pytest.raises(sqlite3.Error):
            conn.execute(
                "UPDATE sessions SET settle_amount=? WHERE id=?", (good + 9.99, sid)
            )
    finally:
        conn.close()

    # 异算法标识本身必须被巡检识别为口径混用。
    findings = helper.sweep()
    hit = [f for f in findings if f["会话编号"] == sid]
    assert hit and "算法标识混用" in hit[0]["问题"]
    # 所有成功审计行的算法标识全局唯一。
    with db() as conn:
        algos = {
            r["algorithm"]
            for r in conn.execute(
                "SELECT DISTINCT algorithm FROM amount_audits WHERE result='成功'"
            ).fetchall()
        }
    assert algos == {AMOUNT_ALGORITHM}


# ---------- 已关账金额 DB 冻结：禁止只改金额展示 ----------
def test_closed_session_amount_frozen_at_db(helper, db_path):
    sid, price = helper.open_with_pulse(kwh=6.0)
    helper.settle(sid)
    import sqlite3

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE sessions SET amount=999 WHERE id=?", (sid,))
    finally:
        conn.close()
    se = helper.session(sid)
    assert se["amount"] == replay_settle_amount(6.0, price)
    assert helper.sweep() == []


# ---------- 重放与舍入自洽：任意时刻重算必须等于落账 ----------
@pytest.mark.parametrize("kwh,price", [(0.1, 0.9), (3.333, 1.2), (10.005, 0.55), (7.777, 1.6)])
def test_replay_rounding_half_up(helper, kwh, price):
    sid, locked = helper.open_with_pulse(kwh=0)
    # 直接构造锁定电价与电量，验证纯函数与落账一致。
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET price_locked=?, kwh=?, amount=? WHERE id=?",
            (price, kwh, replay_settle_amount(kwh, price), sid),
        )
    out = helper.settle(sid)
    assert out["关账金额"] == replay_settle_amount(kwh, price)
    assert out["关账金额"] == round2(kwh * price)
    assert helper.session(sid)["settle_amount"] == replay_settle_amount(kwh, price)


# ---------- 关账后脉冲被拒，落账不可再被电量路径改写 ----------
def test_no_pulse_after_settle(helper):
    sid, _ = helper.open_with_pulse(kwh=3.0)
    before = helper.settle(sid)["关账金额"]
    with pytest.raises(HTTPException) as ei:
        with db() as conn:
            svc.add_pulse(conn, sid, 5.0)
    assert ei.value.status_code == 400
    assert helper.session(sid)["settle_amount"] == before
    assert helper.sweep() == []
