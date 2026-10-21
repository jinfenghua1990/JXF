"""应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .api import open_router, router
from .config import settings
from .database import Base, SessionLocal, engine
from .migrate import ensure_schema
from .models import User
from .security import hash_password
from .services.frontend import frontend_version

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("claims")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    # 已有库补列（create_all 不改表结构）：账号渠道/角色、事件登记人。
    ensure_schema(engine)
    # 首次启动：按 ADMIN 配置播种一个管理员账号（已有账号则跳过）
    if not settings.app_secret:
        log.warning("APP_SECRET 未设置，token 使用临时默认密钥，外网暴露前务必修改！")
    with SessionLocal() as s:
        if s.scalar(select(func.count()).select_from(User)) == 0:
            email = (settings.admin_email or "admin@claims.local").strip().lower()
            pw = settings.admin_password or settings.app_password
            if not pw:
                log.error(
                    "未配置 ADMIN_PASSWORD / APP_PASSWORD，无法创建初始管理员账号。"
                    "系统将拒绝一切登录（fail-closed）。请在 .env 设置管理员密码后重启。"
                )
            else:
                s.add(User(email=email, password_hash=hash_password(pw), is_admin=True,
                           role="admin", channel="operations", display_name="管理员"))
                s.commit()
                log.info("已创建初始管理员账号：%s（请用密码登录）", email)
    log.info("%s v%s 启动  port=%s  db=%s  wdt=%s",
             settings.app_name, settings.app_version, settings.app_port,
             settings.database_url.split("://")[0],
             "已配置" if settings.wdt_sid else "未配置")
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.include_router(router)
app.include_router(open_router)
from .manual_api import router as manual_router
app.include_router(manual_router)

_static = Path(__file__).parent / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": settings.app_version}


@app.get("/")
def index():
    """首页。前端在开发期频繁改动，必须禁缓存，否则浏览器会一直用旧副本，
    表现为「代码明明改了、页面却还是老样子」甚至「登录框又冒出来」。"""
    f = _static / "index.html"
    if f.exists():
        version = frontend_version()
        document = f.read_text(encoding="utf-8").replace("__CLAIMS_PAGE_REVISION__", version["revision"]).replace("__CLAIMS_PAGE_UPDATED__", version["updated_at"])
        return HTMLResponse(
            document,
            headers={
                # 禁缓存（no-store 最强，must-revalidate 兜底）
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                # 内容变了就一定会重新拉（配合上面的 no-store 双保险）
                "ETag": f'W/"{version["revision"]}"',
            },
        )
    return JSONResponse({"ok": True, "msg": "前端未安装"})


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"ok": False, "detail": str(exc)})
