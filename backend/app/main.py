from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.config import PROJECT_NAME
from app.db import LOCKED_DETAIL, WriteBusy
from app.routers import audit, dashboard, dayseal, guns, piles, rates, sessions, settings, sites
from app.seed import seed_if_empty

seed_if_empty()

app = FastAPI(title=PROJECT_NAME)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(WriteBusy)
def write_busy_handler(_: Request, exc: WriteBusy):
    # 写口抢锁失败统一 409：本笔已回滚，调用方重试即可，绝不返回半截终态。
    return JSONResponse(
        status_code=409,
        content={"detail": f"{LOCKED_DETAIL}（{exc}）"},
    )
app.include_router(dashboard.router, prefix="/api")
app.include_router(sites.router, prefix="/api")
app.include_router(piles.router, prefix="/api")
app.include_router(guns.router, prefix="/api")
app.include_router(rates.router, prefix="/api")
app.include_router(sessions.router, prefix="/api")
app.include_router(dayseal.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(audit.router, prefix="/api")


@app.get("/api/健康")
def health():
    return {"产品": PROJECT_NAME, "状态": "正常"}
