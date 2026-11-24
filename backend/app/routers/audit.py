from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import audit as svc

router = APIRouter()


@router.get("/金额审计")
def list_audits(会话编号: int | None = None):
    return svc.list_audits(会话编号)


@router.get("/金额审计/巡检")
def sweep():
    """全量重放巡检：返回所有口径分叉的会话，空列表表示全局闭合。"""
    with db() as conn:
        findings = svc.sweep_amounts(conn)
    return {"异常数": len(findings), "异常明细": findings}
