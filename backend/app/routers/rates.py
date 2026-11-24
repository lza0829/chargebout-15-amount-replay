from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.db import db
from app.services import core
from app.services import rates as svc

router = APIRouter()


class RateIn(BaseModel):
    名称: str = Field(min_length=1)
    峰价: float
    平价: float
    谷价: float


class RatePriceIn(BaseModel):
    峰价: float = Field(ge=0)
    平价: float = Field(ge=0)
    谷价: float = Field(ge=0)


@router.get("/费率")
def list_rates():
    with db() as conn:
        return core.list_rates(conn)


@router.post("/费率")
def create_rate(body: RateIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO rate_plans(name,peak_price,flat_price,valley_price) VALUES(?,?,?,?)",
            (body.名称, body.峰价, body.平价, body.谷价),
        )
        return {"编号": cur.lastrowid, "名称": body.名称}


@router.put("/费率/{rate_id}/档价")
def update_rate(rate_id: int, body: RatePriceIn):
    # 改档价与关账同为写口，立即持锁串行化，杜绝并发半截。
    with db(immediate=True) as conn:
        return svc.update_rate(conn, rate_id, body.峰价, body.平价, body.谷价)
