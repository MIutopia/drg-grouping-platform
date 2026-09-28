"""Excel report export.

Finance / medical affairs need Excel when submitting materials; the hospital has
listed "export" as a requirement to confirm (docs/29 §九). Implementation notes:

- Single entry point `/api/export/{kind}`; each kind declares its allowed roles
  and SQL, so download logic is not duplicated in every domain API;
- Role guard reuses `auth.require_roles`, same source as page permissions;
- Chinese column headers (SQL returns English field names, renamed on export);
- Every export writes `sys_audit_log` - patient-level data leaving the system
  must be traceable (docs/28 security);
- Filenames use `filename*=UTF-8''` encoding, otherwise Chinese names garble in
  some browsers.
"""

from __future__ import annotations

import io
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

import db
from auth import require_roles

router = APIRouter(prefix="/api/export", tags=["export"])

# Spreadsheet formula injection (docs/35 D9): openpyxl marks a string that starts with "="
# as a formula cell, and Excel may also evaluate "+", "-" and "@" prefixed text once the file
# is opened or re-saved as CSV. Exported text is therefore forced to the plain string cell
# type, so a department or group name like "=SUM(A1)" is shown verbatim and never executed.
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


# kind -> (allowed roles, export filename, SQL, column rename map)
# %s in SQL is only used by insim_mine (filtered by the logged-in doctor's phone).
SPECS: dict[str, tuple[tuple[str, ...], str, str, dict[str, str] | None]] = {
    "settlement": (
        ("finance", "cashier"),
        "结算台账",
        "SELECT TOP 3000 medical_no, office_name, drg_code, drg_name, weight, drg_standard, "
        "total_cost, profit_loss, actual_days, settle_date, insurance_type "
        "FROM result_settlement_return ORDER BY settle_date, medical_no",
        {"medical_no": "住院号", "office_name": "科室", "drg_code": "组码", "drg_name": "病组名称",
         "weight": "权重", "drg_standard": "支付标准", "total_cost": "总费用",
         "profit_loss": "盈亏", "actual_days": "住院天数", "settle_date": "结算日期",
         "insurance_type": "险种"},
    ),
    "recon": (
        ("finance", "cashier"),
        "收费对账",
        "SELECT TOP 3000 medical_no, drg_code, total_cost, his_fee, diff, diff_type, "
        "drg_standard, profit_loss FROM result_recon_case ORDER BY ABS(diff) DESC",
        {"medical_no": "住院号", "drg_code": "组码", "total_cost": "平台总费用",
         "his_fee": "HIS费用", "diff": "差异", "diff_type": "差异类型",
         "drg_standard": "支付标准", "profit_loss": "盈亏"},
    ),
    # The three profit columns come with migration 11 (docs/35 D5): they let Finance explain
    # why 盈亏 does not equal 支付标准-总费用 (官方对高低倍率病例置 0). The new backend
    # version therefore requires s18 --sql 11_result_pnl_profit_columns.sql to have been run.
    "pnl_drg": (
        ("finance", "cashier", "manager"),
        "病组盈亏",
        "SELECT key_name, cases, total_cost, std_cost, profit_loss, profit_formula, "
        "profit_adjust, profit_basis, cmi, tcm_share, is_tcm_group "
        "FROM result_pnl WHERE level='drg' ORDER BY profit_loss DESC",
        {"key_name": "病组", "cases": "例数", "total_cost": "总费用", "std_cost": "支付标准",
         "profit_loss": "盈亏", "profit_formula": "盈亏公式值", "profit_adjust": "盈亏调整",
         "profit_basis": "盈亏口径", "cmi": "CMI", "tcm_share": "中医占比",
         "is_tcm_group": "中医优势病组"},
    ),
    "pnl_office": (
        ("finance", "cashier", "manager"),
        "科室盈亏",
        "SELECT key_name, cases, total_cost, std_cost, profit_loss, profit_formula, "
        "profit_adjust, profit_basis, cmi, tcm_share "
        "FROM result_pnl WHERE level='office' ORDER BY profit_loss DESC",
        {"key_name": "科室", "cases": "例数", "total_cost": "总费用", "std_cost": "支付标准",
         "profit_loss": "盈亏", "profit_formula": "盈亏公式值", "profit_adjust": "盈亏调整",
         "profit_basis": "盈亏口径", "cmi": "CMI", "tcm_share": "中医占比"},
    ),
    "pnl_month": (
        ("finance", "cashier", "manager"),
        "月度盈亏",
        "SELECT period, cases, total_cost, std_cost, profit_loss, profit_formula, "
        "profit_adjust, profit_basis, tcm_share "
        "FROM result_pnl WHERE level='hospital' ORDER BY period",
        {"period": "月份", "cases": "例数", "total_cost": "总费用", "std_cost": "支付标准",
         "profit_loss": "盈亏", "profit_formula": "盈亏公式值", "profit_adjust": "盈亏调整",
         "profit_basis": "盈亏口径", "tcm_share": "中医占比"},
    ),
    "compare": (
        ("manager",),
        "对表明细",
        "SELECT TOP 3000 medical_no, grp_official, grp_vendor, grp_engine, adrg_official, "
        "adrg_engine, agree_adrg, agree_drg, total_cost, profit_loss "
        "FROM result_compare_case ORDER BY medical_no",
        {"medical_no": "住院号", "grp_official": "官方组码", "grp_vendor": "既有预分组",
         "grp_engine": "引擎组码", "adrg_official": "官方ADRG", "adrg_engine": "引擎ADRG",
         "agree_adrg": "ADRG一致", "agree_drg": "四位码一致", "total_cost": "总费用",
         "profit_loss": "盈亏"},
    ),
    "insim_mine": (
        ("doctor",),
        "我的在院患者",
        "SELECT hospital_no, days, main_dx_name, drg_code, std_cost, fee_to_date, delta, "
        "line1_group, line2_gap, line3_cost, line4_advice FROM result_insim "
        "WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM result_insim) "
        "AND doctor_phone=%s ORDER BY days DESC",
        {"hospital_no": "住院号", "days": "住院天数", "main_dx_name": "主要诊断",
         "drg_code": "预分组", "std_cost": "支付标准", "fee_to_date": "当前费用",
         "delta": "预计盈亏", "line1_group": "①当前预分组", "line2_gap": "②入组差距",
         "line3_cost": "③费用预判", "line4_advice": "④优化空间"},
    ),
    "audit": (
        ("it",),
        "访问审计",
        "SELECT TOP 5000 at, user_phone, action, target, detail, ip FROM sys_audit_log "
        "ORDER BY id DESC",
        {"at": "时间", "user_phone": "用户", "action": "动作", "target": "对象",
         "detail": "详情", "ip": "IP"},
    ),
    "alerts": (
        ("it",),
        "告警清单",
        "SELECT id, severity, item, detail, status, owner, acked_by, raised_at "
        "FROM ops_alert ORDER BY id DESC",
        {"id": "编号", "severity": "级别", "item": "事项", "detail": "详情",
         "status": "状态", "owner": "责任方", "acked_by": "确认人", "raised_at": "提出时间"},
    ),
}


def _xlsx(rows: list[dict], rename: dict[str, str] | None) -> io.BytesIO:
    df = pd.DataFrame(rows)
    if rename:
        keep = [c for c in rename if c in df.columns]
        df = df[keep].rename(columns=rename)

    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append([str(c) for c in df.columns])
    for r_i, rec in enumerate(df.itertuples(index=False, name=None), start=2):
        for c_i, value in enumerate(rec, start=1):
            cell = ws.cell(row=r_i, column=c_i)
            cell.value = value
            if isinstance(value, str) and value[:1] in _FORMULA_PREFIX:
                cell.data_type = "s"          # verbatim text, never a formula

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@router.get("/kinds")
def kinds(user: dict = Depends(require_roles("finance", "cashier", "manager", "doctor", "it"))) -> dict:
    """Report kinds the current role may export (the frontend shows buttons accordingly)."""
    return {"kinds": {k: v[1] for k, v in SPECS.items() if user["role_code"] in v[0]}}


@router.get("/{kind}")
def export(kind: str, user: dict = Depends(require_roles(
        "finance", "cashier", "manager", "doctor", "it"))):
    if kind not in SPECS:
        raise HTTPException(404, f"未知报表类型：{kind}")
    roles, title, sql, rename = SPECS[kind]
    if user["role_code"] not in roles:
        raise HTTPException(403, "当前角色无权导出该报表")

    params: tuple = ()
    if kind == "insim_mine":
        params = (user["login_phone"],)
    rows = db.records(db.query(sql, params))
    db.audit(user["login_phone"], "export", target=kind, detail=f"{len(rows)} 行")

    buf = _xlsx(rows, rename)
    fname = f"{title}_{pd.Timestamp.now():%Y%m%d}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"},
    )
