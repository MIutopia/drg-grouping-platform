"""DRG multi-role workbench - backend API (FastAPI).

This file: login / me / doctor workbench; see ops_api.py for the IT console.
All data comes from the drg database; no recomputation, no HIS reads.

Run:
    pip install -r src/web/backend/requirements.txt
    python src/web/backend/seed_users.py          # create accounts (initial passwords)
    uvicorn app:app --app-dir src/web/backend --reload --port 8000

Environment variables:
    DRG_WEB_SECRET   token signing secret; MUST be set explicitly in production
                     (see security.py; a random one is generated with a warning otherwise)
    DRG_WEB_ORIGINS  allowed frontend origins, comma-separated; local Vite dev only by default
    DRG_WEB_ORIGIN_REGEX  extra Origin regex for browser CORS; defaults to the hospital
                     LAN segments 192.0.2.0/24 and 198.51.100.0/24, set empty to disable
    DRG_LOGIN_MAX    login failure limit (default 5)
    DRG_LOGIN_WINDOW failure window in seconds (default 600)

Docs: docs/29-多角色工作台平台方案.md
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pymssql
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import security  # noqa: E402
from auth import current_user  # noqa: E402
from finance_api import router as finance_router  # noqa: E402
from jobs_api import router as jobs_router  # noqa: E402
from manager_api import router as manager_router  # noqa: E402
from ops_api import router as ops_router  # noqa: E402
from export_api import router as export_router  # noqa: E402

app = FastAPI(title="DRG 多角色工作台 API", version="0.2")


def _allowed_origins() -> list[str]:
    """CORS allowlist. Only local dev origins by default.

    Never combine `allow_origins=["*"]` with `allow_credentials=True`: browsers reject
    that combination, and semantically it opens credentialed access to every site.
    """
    raw = os.environ.get(
        "DRG_WEB_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [o.strip() for o in raw.split(",") if o.strip()]


def _allowed_origin_regex() -> str | None:
    """Browser CORS only: allow Origins whose host is in the hospital internal LAN
    segment (192.0.2.0/24 and 198.51.100.0/24). Override via DRG_WEB_ORIGIN_REGEX;
    set empty to disable the regex branch.

    This is NOT an access-control layer: it only lets browsers on those hosts call
    the API with credentials. Server/tool access is unaffected (use firewall or an
    app-level IP allowlist for that).
    """
    return os.environ.get(
        "DRG_WEB_ORIGIN_REGEX",
        r"^https?://(194\.100\.0\.\d{1,3}|104\.100\.0\.\d{1,3})(:\d+)?$")


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_allowed_origin_regex(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---- Login rate limiting ------------------------------------------------
# Count failures per phone+IP and reject temporarily above the limit to stop
# brute force. In-memory counters (single process).
_MAX_FAIL = int(os.environ.get("DRG_LOGIN_MAX", "5"))
_WINDOW = int(os.environ.get("DRG_LOGIN_WINDOW", "600"))
_fails: dict[str, list[float]] = {}
_fails_lock = threading.Lock()


def _rate_key(phone: str, ip: str) -> str:
    return f"{phone}|{ip}"


def _over_limit(key: str) -> bool:
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(key, []) if now - t < _WINDOW]
        _fails[key] = recent
        return len(recent) >= _MAX_FAIL


def _record_fail(key: str) -> None:
    with _fails_lock:
        _fails.setdefault(key, []).append(time.time())


def _clear_fail(key: str) -> None:
    with _fails_lock:
        _fails.pop(key, None)


class LoginIn(BaseModel):
    phone: str
    password: str


@app.exception_handler(pymssql.OperationalError)
def _db_unavailable(request: Request, exc: pymssql.OperationalError) -> JSONResponse:
    """A downed data source is not an application error (docs/35 D7).

    Without this, a stopped database surfaced as HTTP 500 and the frontend showed a
    white-screen error. 503 + a plain message lets the UI say "数据源不可用，请稍后重试".
    """
    return JSONResponse(statu列(code)=503,
                        content={"detail": "数据源不可用，请稍后重试"},
                        headers={"Retry-After": "15"})


@app.get("/api/health")
def health() -> dict:
    """Liveness plus dependency status.

    Stays HTTP 200 so external probes keep distinguishing "process up" from "process
    dead"; the `db` field reports whether the data source is reachable right now
    (measured with a short-timeout probe, so a stopped DB cannot make this hang).
    """
    db_ok = db.ping()
    return {"ok": True, "service": "drg-workbench", "db": db_ok,
            "status": "ok" if db_ok else "degraded"}


@app.post("/api/login")
def login(body: LoginIn, request: Request) -> dict:
    phone = body.phone.strip()
    ip = request.client.host if request.client else ""
    key = _rate_key(phone, ip)
    if _over_limit(key):
        db.audit(phone, "login_blocked", ip=ip, detail=f"{_MAX_FAIL} 次失败")
        raise HTTPException(429, f"登录失败次数过多，请 {_WINDOW // 60} 分钟后再试")

    u = db.query("SELECT login_phone, user_name, pwd_hash FROM sys_user "
                 "WHERE login_phone = %s AND active = 1", (phone,))
    if u.empty or not security.verify_pwd(body.password, u.iloc[0]["pwd_hash"]):
        _record_fail(key)
        db.audit(phone, "login_fail", ip=ip)
        raise HTTPException(401, "手机号或密码错误")

    _clear_fail(key)
    db.execute("UPDATE sys_user SET last_login_at = GETDATE() WHERE login_phone = %s", (phone,))
    db.audit(phone, "login", ip=ip)
    return {"token": security.make_token(phone), "name": u.iloc[0]["user_name"]}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"phone": user["login_phone"], "name": user["user_name"],
            "role": user["role_code"], "role_name": user["role_name"],
            "office": user["office"]}


@app.get("/api/insim/mine")
def insim_mine(date: str | None = Query(None),
               user: dict = Depends(current_user)) -> dict:
    """Doctor workbench: the four-line simulation rows for my in-hospital
    patients (latest snapshot by default)."""
    params: list = [user["login_phone"]]
    cond_date = "(SELECT MAX(snapshot_date) FROM result_insim)"
    if date:
        cond_date = "%s"
        params.insert(0, date)
    rows = db.query(
        "SELECT hospital_no AS 住院号, days AS 住院天数, main_dx AS 主要诊断编码, "
        "main_dx_name AS 主要诊断, op_classes AS 中医操作类, tcm_ratio AS 中治率, "
        "path_basis AS 分组路径, drg_code AS 预分组, weight AS 权重, std_cost AS 支付标准, "
        "fee_to_date AS 当前费用, delta AS 预计盈亏, "
        "line1_group AS 当前预分组, line2_gap AS 入组差距, "
        "line3_cost AS 费用预判, line4_advice AS 优化空间, snapshot_date AS 快照日 "
        f"FROM result_insim WHERE snapshot_date = {cond_date} AND doctor_phone = %s "
        "ORDER BY days DESC", tuple(params))
    db.audit(user["login_phone"], "view", target="insim/mine", detail=f"{len(rows)} 例")
    return {"count": len(rows), "items": db.records(rows)}


class PwdIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/account/password")
def change_password(body: PwdIn, user: dict = Depends(current_user)) -> dict:
    """Change my own login password (post-login account feature). No forced
    change on first login; users update it themselves."""
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    u = db.query("SELECT pwd_hash FROM sys_user WHERE login_phone=%s", (user["login_phone"],))
    if u.empty or not security.verify_pwd(body.old_password, u.iloc[0]["pwd_hash"]):
        raise HTTPException(400, "原密码不正确")
    db.execute("UPDATE sys_user SET pwd_hash=%s WHERE login_phone=%s",
               (security.hash_pwd(body.new_password), user["login_phone"]))
    db.audit(user["login_phone"], "change_pwd")
    return {"ok": True}


app.include_router(ops_router)
app.include_router(jobs_router)
app.include_router(finance_router)
app.include_router(manager_router)
app.include_router(export_router)
