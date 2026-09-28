"""Medical affairs dashboard API: hospital P&L, compare quality, whitelist review,
regression, and DRG settlement list upload.

Data comes from the drg tables result_pnl / result_compare_case / ops_regress_run,
plus src/config/whitelist_rules.csv (whitelist review state - not in the database
yet, read directly from the file). Access restricted to role='manager'.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

import db
from auth import require_roles

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))
from settings import CONFIG_DIR  # noqa: E402

router = APIRouter(prefix="/api/manager", tags=["manager"])
MGR = require_roles("manager")


def _rows(sql: str, params: tuple | None = None) -> list[dict]:
    return db.records(db.query(sql, params))


def _whitelist() -> list[dict]:
    p = CONFIG_DIR / "whitelist_rules.csv"
    if not p.exists():
        return []
    d = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
    cols = ["rule_id", "category", "rule_text", "status", "confirmed_by", "confirmed_date"]
    return d[[c for c in cols if c in d.columns]].to_dict(orient="records")


@router.get("/overview")
def overview(user: dict = Depends(MGR)) -> dict:
    periods = _rows("SELECT period, cases, total_cost, std_cost, profit_loss, tcm_share "
                    "FROM result_pnl WHERE level='hospital' ORDER BY period")
    # Compute agreement only over cases that have an engine prediction: a few cases
    # yield no prediction because the source table lacks diagnosis codes; counting
    # them in the denominator would treat them as "disagree" and unfairly depress
    # the rate (10 of N cases).
    c = db.query("SELECT COUNT(*) n, SUM(CASE WHEN agree_adrg=1 THEN 1 ELSE 0 END) adrg_ok, "
                 "SUM(CASE WHEN agree_drg=1 THEN 1 ELSE 0 END) drg_ok FROM result_compare_case "
                 "WHERE adrg_engine IS NOT NULL AND adrg_engine <> ''")
    n = int(c["n"].iloc[0])
    adrg_ok, drg_ok = int(c["adrg_ok"].iloc[0]), int(c["drg_ok"].iloc[0])
    wl = _whitelist()
    reg = _rows("SELECT TOP 1 run_id, run_at, verdict, adrg_rate, drg_rate "
                "FROM ops_regress_run ORDER BY run_id DESC")
    db.audit(user["login_phone"], "view", target="manager/overview")
    return {
        # pad periods to "the last 6 months including the current one" (no-data months left empty)
        "periods": db.pad_months(periods), "latest": periods[-1] if periods else None,
        "compare": {"n": n, "adrg_ok": adrg_ok, "drg_ok": drg_ok,
                    "adrg_rate": (adrg_ok / n if n else None),
                    "drg_rate": (drg_ok / n if n else None)},
        "paths": _rows("SELECT path, COUNT(*) n FROM result_compare_case GROUP BY path"),
        "whitelist": {"total": len(wl), **Counter(r["status"] for r in wl)},
        "last_regress": reg[0] if reg else None,
    }


@router.get("/pnl")
def pnl(level: str = Query("drg"), user: dict = Depends(MGR)) -> list[dict]:
    if level not in ("drg", "office", "hospital"):
        level = "drg"
    return _rows("SELECT level, period, key_code, key_name, cases, total_cost, std_cost, "
                 "profit_loss, cmi, tcm_share, is_tcm_group FROM result_pnl WHERE level=%s "
                 "ORDER BY profit_loss DESC", (level,))


@router.get("/whitelist")
def whitelist(user: dict = Depends(MGR)) -> list[dict]:
    return _whitelist()


# ---- Whitelist review (manual add/delete/review, owned by the medical affairs office) ----
# Whitelist lives in config/whitelist_rules.csv (read by output_guard at runtime;
# only confirmed rules take effect). These endpoints are the platform-side entry
# for editing it, equivalent to hand-editing the CSV but with audit trails.
def _wl_path() -> Path:
    return CONFIG_DIR / "whitelist_rules.csv"


def _wl_df() -> "pd.DataFrame":
    p = _wl_path()
    cols = ["rule_id", "category", "rule_text", "status", "confirmed_by", "confirmed_date"]
    if not p.exists():
        return pd.DataFrame(columns=cols)
    return pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")


def _wl_save(df: "pd.DataFrame") -> None:
    _wl_path().parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(_wl_path(), index=False, encoding="utf-8-sig")


class WhitelistItem(BaseModel):
    rule_id: str
    category: str
    rule_text: str
    status: str = "待审议"


class WhitelistStatus(BaseModel):
    status: str


@router.post("/whitelist")
def whitelist_add(item: WhitelistItem, user: dict = Depends(MGR)) -> dict:
    """Add a whitelist rule (medical affairs only). New rules start in the
    pending-review status and take effect only after confirmation."""
    df = _wl_df()
    if item.rule_id in set(df["rule_id"].tolist()):
        raise HTTPException(400, "rule_id 已存在")
    new = {"rule_id": item.rule_id, "category": item.category, "rule_text": item.rule_text,
           "status": item.status or "待审议", "confirmed_by": "", "confirmed_date": ""}
    df = pd.concat([df, pd.DataFrame([new])], ignore_index=True)
    _wl_save(df)
    db.audit(user["login_phone"], "whitelist_add", target=item.rule_id)
    return {"ok": True}


@router.put("/whitelist/{rule_id}")
def whitelist_set(rule_id: str, body: WhitelistStatus, user: dict = Depends(MGR)) -> dict:
    """Review a rule: mark confirmed (records reviewer/date) or revert to pending."""
    df = _wl_df()
    if rule_id not in set(df["rule_id"].tolist()):
        raise HTTPException(404, "规则不存在")
    df.loc[df["rule_id"] == rule_id, "status"] = body.status
    if body.status == "已确认":
        df.loc[df["rule_id"] == rule_id, "confirmed_by"] = user["user_name"]
        df.loc[df["rule_id"] == rule_id, "confirmed_date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        df.loc[df["rule_id"] == rule_id, "confirmed_by"] = ""
        df.loc[df["rule_id"] == rule_id, "confirmed_date"] = ""
    _wl_save(df)
    db.audit(user["login_phone"], "whitelist_set", target=rule_id, detail=body.status)
    return {"ok": True}


@router.delete("/whitelist/{rule_id}")
def whitelist_del(rule_id: str, user: dict = Depends(MGR)) -> dict:
    """Delete a whitelist rule (medical affairs only)."""
    df = _wl_df()
    if rule_id not in set(df["rule_id"].tolist()):
        raise HTTPException(404, "规则不存在")
    df = df[df["rule_id"] != rule_id]
    _wl_save(df)
    db.audit(user["login_phone"], "whitelist_del", target=rule_id)
    return {"ok": True}


# ---- DRG settlement list upload (data ingestion, owned by the medical affairs office) ----
# s01 globs the staging folder (原始资料/DRG结算样表) every month; the medical affairs
# office uploads here and the next settlement-import job auto-picks it up.
# Only the manager role may upload.
STAGING = Path(__file__).resolve().parents[3] / "原始资料" / "DRG结算样表"
ALLOWED_EXT = {".xlsx", ".xls", ".csv", ".pdf", ".docx", ".doc"}
MAX_BYTES = 50 * 1024 * 1024


@router.post("/upload")
async def upload_settlement(file: UploadFile = File(...), user: dict = Depends(MGR)) -> dict:
    """Upload the monthly DRG settlement list (medical affairs only). Validates
    extension/size/filename; blocks path traversal and overwrite."""
    raw = file.filename or ""
    name = Path(raw).name            # keep basename only, strip any dir segments (blocks ../ traversal)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型：{ext or '无扩展名'}")
    if not name or name in (".", ".."):
        raise HTTPException(400, "文件名无效")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"文件过大（约 {len(data)//1024//1024}MB），上限 50MB")
    STAGING.mkdir(parents=True, exist_ok=True)
    dest = STAGING / name
    if dest.exists():                # never overwrite: append a timestamp
        stem = Path(name).stem
        dest = STAGING / f"{stem}_{datetime.now():%Y%m%d%H%M%S}{ext}"
    with open(dest, "wb") as fh:
        fh.write(data)
    db.audit(user["login_phone"], "upload", target=str(dest), detail=f"{len(data)} bytes")
    return {"ok": True, "saved": dest.name, "size": len(data)}
