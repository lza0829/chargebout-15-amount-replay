"""全局唯一关账金额口径（Arm B：关账瞬间按锁定电价重算）。

本模块是全系统唯一的金额算法出处：脉冲预估列、关账落账、回执、详情、
总览合计、审计巡检都必须经由这里的函数，禁止在别处另起金额公式。

口径定义（全局不得混用另一种算法）：
    关账金额 = round2(会话锁定电价 × 累计电量)
预估金额列只是同一口径下的浏览值，不决定关账结果。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 写入每一条金额审计行的算法标识；巡检据此识别是否存在两种算法混用。
AMOUNT_ALGORITHM = "LOCKED_PRICE_x_KWH_HALF_UP"

# 分位容差：展示值与重放值差异超过 1 分钱即判定口径分叉。
AMOUNT_TOLERANCE = 0.005


def round2(value: float | Decimal | str) -> float:
    """全局唯一舍入：四舍五入到分（ROUND_HALF_UP），臂内自洽。"""
    return float(
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def replay_settle_amount(kwh: float, price_locked: float) -> float:
    """关账金额的可重放纯函数。

    任何时刻，只要给出会话的累计电量与锁定电价，重放结果必须等于
    该会话实际落账的关账金额；否则即为口径混用或旁路篡改。
    """
    return round2(Decimal(str(kwh)) * Decimal(str(price_locked)))


def amounts_match(shown: float, replay: float) -> bool:
    """预估展示值与重放值是否同口径（1 分容差）。"""
    return abs(float(shown) - float(replay)) <= AMOUNT_TOLERANCE
