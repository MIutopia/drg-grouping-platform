"""IT console API (docs/29): alerts / jobs / regression / freshness / policy ledger / admin.

Read-mostly; the only write operation is "acknowledge alert". Data comes from the
drg tables ops_* (produced by s19/s20), dict_policy_* (s21) and sys_* (platform tables).
The whole block is restricted to role_code='it'.
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import db
import security
from auth import require_roles

router = APIRouter(prefix="/api/ops", tags=["ops"])
IT = require_roles("it")


def _rows(sql: str, params: tuple | None = None) -> list[dict]:
    # Do not just fillna(""): NaT (e.g. a never-logged-in last_login_at) makes FastAPI
    # raise 500 while serializing; db.records normalizes NaT/NaN to null uniformly.
    return db.records(db.query(sql, params))


def _one(sql: str, params: tuple | None = None) -> dict | None:
    r = _rows(sql, params)
    return r[0] if r else None


def _count(sql: str, params: tuple | None = None) -> int:
    return int(db.query(sql, params)["n"].iloc[0])


@router.get("/overview")
def overview(user: dict = Depends(IT)) -> dict:
    """Console home overview: open alerts, recent jobs, latest regression, freshness anomalies."""
    db.audit(user["login_phone"], "view", target="ops/overview")
    return {
        "open_alerts": _count("SELECT COUNT(*) n FROM ops_alert WHERE status='open'"),
        "bad_fresh": _count("SELECT COUNT(*) n FROM ops_freshness WHERE status <> 'ok'"),
        "policy_docs": _count("SELECT COUNT(*) n FROM dict_policy_doc"),
        "users": _count("SELECT COUNT(*) n FROM sys_user WHERE active=1"),
        "last_run": _one("SELECT TOP 1 job_name, status, rows_out, ended_at, message "
                         "FROM ops_run_log ORDER BY id DESC"),
        "last_regress": _one("SELECT TOP 1 run_id, run_at, verdict, adrg_rate, drg_rate, "
                             "delta_adrg, delta_drg FROM ops_regress_run ORDER BY run_id DESC"),
    }


@router.get("/alerts")
def alerts(status: str | None = None, user: dict = Depends(IT)) -> list[dict]:
    cols = ("id, raised_at, severity, item, detail, status, [owner], acked_by, acked_at")
    if status:
        return _rows(f"SELECT {cols} FROM ops_alert WHERE status=%s ORDER BY id DESC", (status,))
    return _rows(f"SELECT {cols} FROM ops_alert ORDER BY id DESC")


@router.post("/alerts/{aid}/ack")
def ack_alert(aid: int, user: dict = Depends(IT)) -> dict:
    db.execute("UPDATE ops_alert SET status='ack', acked_by=%s, acked_at=GETDATE() "
               "WHERE id=%s AND status='open'", (user["user_name"], aid))
    db.audit(user["login_phone"], "ack_alert", target=str(aid))
    return {"ok": True}


@router.get("/runs")
def runs(user: dict = Depends(IT)) -> list[dict]:
    return _rows("SELECT TOP 50 id, job_name, started_at, ended_at, status, rows_out, message "
                 "FROM ops_run_log ORDER BY id DESC")


@router.get("/regress")
def regress(user: dict = Depends(IT)) -> dict:
    """Regression board: the 20 most recent runs plus per-month detail of the latest one."""
    runs_ = _rows("SELECT TOP 20 run_id, run_at, cases, adrg_rate, drg_rate, th_adrg, th_drg, "
                  "verdict, delta_adrg, delta_drg, note FROM ops_regress_run ORDER BY run_id DESC")
    metrics: list[dict] = []
    if runs_:
        metrics = _rows("SELECT dim, cases, adrg_rate, drg_rate, tcm_cases, std_cases, none_cases "
                        "FROM ops_regress_metric WHERE run_id=%s ORDER BY dim", (runs_[0]["run_id"],))
    return {"runs": runs_, "latest_run_id": runs_[0]["run_id"] if runs_ else None, "metrics": metrics}


@router.get("/freshness")
def freshness(user: dict = Depends(IT)) -> list[dict]:
    return _rows("SELECT observed_at, source_db, source_table, business_ts, rows_total, "
                 "lag_hours, status FROM ops_freshness ORDER BY status DESC, source_table")


@router.get("/policy")
def policy(user: dict = Depends(IT)) -> dict:
    """Policy ledger: registered documents plus extracted policy facts (produced by s21)."""
    return {
        "docs": _rows("SELECT TOP 50 id, doc_key, file_name, doc_no, category, ext, pages, "
                      "tables_n, rows_n, sha256, captured_at FROM dict_policy_doc ORDER BY id DESC"),
        "facts": _rows("SELECT doc_key, fact_key, fact_label, fact_value, captured_at "
                       "FROM dict_policy_fact ORDER BY id DESC"),
    }


# Recovery window for soft-deleted accounts: recoverable on the admin page for 15
# days after deletion, permanent afterwards.
RECOVER_DAYS = 15


@router.get("/users")
def users(user: dict = Depends(IT)) -> list[dict]:
    # Show only accounts that are not deleted plus those deleted within 15 days
    # (still recoverable); older deletions are permanent and hidden.
    return _rows("""
        SELECT u.user_id, u.login_phone, u.user_name, r.role_code, r.role_name,
               u.office, u.active, u.last_login_at, u.deleted_at,
               CASE WHEN u.deleted_at IS NOT NULL
                         AND u.deleted_at >= DATEADD(day, -%s, GETDATE())
                    THEN DATEDIFF(hour, GETDATE(), DATEADD(day, %s, u.deleted_at))
                    ELSE NULL END AS recover_hours
        FROM sys_user u JOIN sys_role r ON r.role_id = u.role_id
        WHERE u.deleted_at IS NULL
           OR u.deleted_at >= DATEADD(day, -%s, GETDATE())
        ORDER BY r.role_code, u.user_name
    """, (RECOVER_DAYS, RECOVER_DAYS, RECOVER_DAYS))


@router.get("/roles")
def roles(user: dict = Depends(IT)) -> list[dict]:
    return _rows("SELECT role_id, role_code, role_name FROM sys_role ORDER BY role_id")


@router.get("/audit")
def audit(user: dict = Depends(IT)) -> list[dict]:
    return _rows("SELECT TOP 100 id, [at], user_phone, action, target, detail, ip "
                 "FROM sys_audit_log ORDER BY id DESC")


# ---- Admin: user write operations ---------------------------------------
def _init_pwd() -> str:
    """Initial password for a new (or reset) account - code review S2.

    DRG_INIT_PWD is the hospital's chosen policy and is used verbatim when it is set (the
    test stack injects it). When it is NOT set we generate a random one and hand it back in
    the response instead of falling back to a literal "888888" shared by every account:
    a uniform weak default is a horizontal-movement risk once the platform sits on the
    hospital LAN. The response shape is unchanged - `init_pwd` was already returned to IT.
    """
    return os.environ.get("DRG_INIT_PWD") or secrets.token_urlsafe(9)


class UserIn(BaseModel):
    phone: str
    name: str
    role_code: str
    office: str | None = None


class UserPatch(BaseModel):
    active: int | None = None
    role_code: str | None = None
    office: str | None = None
    user_name: str | None = None
    reset_pwd: bool = False


@router.post("/users")
def create_user(body: UserIn, user: dict = Depends(IT)) -> dict:
    phone = body.phone.strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise HTTPException(400, "手机号格式不正确（应为 11 位）")
    if not db.query("SELECT 1 AS x FROM sys_user WHERE login_phone=%s", (phone,)).empty:
        raise HTTPException(400, "该手机号已存在")
    role = db.query("SELECT role_id FROM sys_role WHERE role_code=%s", (body.role_code,))
    if role.empty:
        raise HTTPException(400, "角色不存在")
    init_pwd = _init_pwd()
    db.execute("INSERT INTO sys_user (login_phone, user_name, role_id, office, pwd_hash) "
               "VALUES (%s, %s, %s, %s, %s)",
               (phone, body.name.strip(), int(role.iloc[0]["role_id"]), body.office,
                security.hash_pwd(init_pwd)))
    db.audit(user["login_phone"], "user_admin", target=phone, detail=f"新增/{body.role_code}")
    return {"ok": True, "init_pwd": init_pwd}


@router.patch("/users/{uid}")
def patch_user(uid: int, body: UserPatch, user: dict = Depends(IT)) -> dict:
    sets: list[str] = []
    params: list = []
    changes: list[str] = []
    if body.role_code is not None:
        role = db.query("SELECT role_id FROM sys_role WHERE role_code=%s", (body.role_code,))
        if role.empty:
            raise HTTPException(400, "角色不存在")
        sets.append("role_id=%s")
        params.append(int(role.iloc[0]["role_id"]))
        changes.append(f"角色->{body.role_code}")
    if body.office is not None:
        sets.append("office=%s")
        params.append(body.office)
        changes.append("改科室")
    if body.user_name is not None and body.user_name.strip():
        sets.append("user_name=%s")
        params.append(body.user_name.strip())
        changes.append("改名")
    if body.active is not None:
        if int(body.active) == 0:
            tgt = db.query("SELECT r.role_code FROM sys_user u JOIN sys_role r ON r.role_id=u.role_id "
                           "WHERE u.user_id=%s", (uid,))
            if not tgt.empty and tgt.iloc[0]["role_code"] == "it":
                n_it = _count("SELECT COUNT(*) n FROM sys_user u JOIN sys_role r ON r.role_id=u.role_id "
                              "WHERE r.role_code='it' AND u.active=1 AND u.user_id <> %s", (uid,))
                if n_it == 0:
                    raise HTTPException(400, "不能停用最后一个信息科账号，否则将无人可管理")
        sets.append("active=%s")
        params.append(int(body.active))
        changes.append("启用" if int(body.active) == 1 else "停用")
    new_pwd: str | None = None
    if body.reset_pwd:
        new_pwd = _init_pwd()
        sets.append("pwd_hash=%s")
        params.append(security.hash_pwd(new_pwd))
        changes.append("重置密码")
    if not sets:
        raise HTTPException(400, "没有需要修改的字段")
    params.append(uid)
    db.execute("UPDATE sys_user SET " + ", ".join(sets) + " WHERE user_id=%s", tuple(params))
    db.audit(user["login_phone"], "user_admin", target=str(uid), detail=";".join(changes))
    out = {"ok": True, "changes": changes}
    if new_pwd:                      # a reset must hand the new password back to IT
        out["init_pwd"] = new_pwd
    return out


@router.delete("/users/{uid}")
def delete_user(uid: int, user: dict = Depends(IT)) -> dict:
    """Soft delete: set deleted_at and deactivate; recoverable on the admin page within 15 days."""
    tgt = db.query("SELECT r.role_code FROM sys_user u JOIN sys_role r ON r.role_id = u.role_id "
                   "WHERE u.user_id = %s", (uid,))
    if tgt.empty:
        raise HTTPException(404, "账号不存在")
    if tgt.iloc[0]["role_code"] == "it":
        n_it = _count(
            "SELECT COUNT(*) n FROM sys_user u JOIN sys_role r ON r.role_id = u.role_id "
            "WHERE r.role_code='it' AND u.user_id <> %s "
            "AND (u.deleted_at IS NULL OR u.deleted_at >= DATEADD(day, -%s, GETDATE()))",
            (uid, RECOVER_DAYS))
        if n_it == 0:
            raise HTTPException(400, "不能删除最后一个信息科账号，否则将无人可管理")
    db.execute("UPDATE sys_user SET active=0, deleted_at=GETDATE() WHERE user_id=%s", (uid,))
    db.audit(user["login_phone"], "user_admin", target=str(uid), detail="删除(15天内可撤回)")
    return {"ok": True}


@router.post("/users/{uid}/restore")
def restore_user(uid: int, user: dict = Depends(IT)) -> dict:
    """Undo a soft delete: clear deleted_at and reactivate; only within 15 days of deletion."""
    row = db.query("SELECT deleted_at FROM sys_user WHERE user_id=%s", (uid,))
    if row.empty:
        raise HTTPException(404, "账号不存在")
    da = row.iloc[0]["deleted_at"]
    if da is None:
        raise HTTPException(400, "该账号未被删除")
    if da < (datetime.now() - timedelta(days=RECOVER_DAYS)):
        raise HTTPException(400, f"已超过 {RECOVER_DAYS} 天撤回期限，无法恢复")
    db.execute("UPDATE sys_user SET active=1, deleted_at=NULL WHERE user_id=%s", (uid,))
    db.audit(user["login_phone"], "user_admin", target=str(uid), detail="撤回删除")
    return {"ok": True}
