"""Finance / cashier workbench API: P&L board / settlement ledger / fee reconciliation.

Data comes from the drg tables result_pnl (three-level P&L from s08),
result_recon_case (per-case reconciliation from s07) and result_settlement_return
(official settlement returns). Restricted to role in (finance, cashier).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

import db
from auth import require_roles

router = APIRouter(prefix="/api/finance", tags=["finance"])
FIN = require_roles("finance", "cashier")


def _rows(sql: str, params: tuple | None = None) -> list[dict]:
    return db.records(db.query(sql, params))


def _count(sql: str, params: tuple | None = None) -> int:
    return int(db.query(sql, params)["n"].iloc[0])


@router.get("/overview")
def overview(user: dict = Depends(FIN)) -> dict:
    """Hospital-level monthly P&L overview.

    periods is padded to "the last 6 months including the current one" with metrics
    left empty for missing months (visible gaps on the chart show which months have
    no data yet); latest always takes the most recent month that actually has data,
    so the stat cards do not go blank when the current month has no settlement yet.
    """
    rows = _rows("SELECT period, cases, total_cost, std_cost, profit_loss, tcm_share "
                 "FROM result_pnl WHERE level='hospital' ORDER BY period")
    db.audit(user["login_phone"], "view", target="finance/overview")
    return {"periods": db.pad_months(rows), "latest": rows[-1] if rows else None,
            "settlement_cases": _count("SELECT COUNT(*) n FROM result_settlement_return")}


@router.get("/pnl")
def pnl(level: str = Query("drg"), user: dict = Depends(FIN)) -> list[dict]:
    """P&L detail: level = drg (group) / office (department)."""
    if level not in ("drg", "office", "hospital"):
        level = "drg"
    return _rows("SELECT level, period, key_code, key_name, cases, total_cost, std_cost, "
                 "profit_loss, cmi, tcm_share, avg_cost, avg_pnl, is_tcm_group "
                 "FROM result_pnl WHERE level=%s ORDER BY profit_loss DESC", (level,))


@router.get("/recon")
def recon(user: dict = Depends(FIN)) -> dict:
    """Fee reconciliation: summary by diff type plus the per-case detail with the largest diffs."""
    return {
        "types": _rows("SELECT diff_type, COUNT(*) n, SUM(profit_loss) pl, SUM(diff) diff_sum "
                       "FROM result_recon_case GROUP BY diff_type ORDER BY n DESC"),
        "cases": _rows("SELECT TOP 200 medical_no, drg_code, total_cost, his_fee, drg_standard, "
                       "diff, diff_type, profit_loss, zzl_ratio FROM result_recon_case "
                       "ORDER BY ABS(diff) DESC"),
    }


@router.get("/settlement")
def settlement(limit: int = Query(200), office: str | None = None,
               user: dict = Depends(FIN)) -> list[dict]:
    """Settlement ledger (official settlement returns, per case)."""
    cond, params = "", ()
    if office:
        cond = "WHERE office_name LIKE %s"
        params = (f"%{office}%",)
    return _rows(f"SELECT TOP {int(limit)} settle_id, medical_no, office_name, drg_code, drg_name, "
                 f"weight, drg_standard, total_cost, profit_loss, actual_days, settle_date, "
                 f"insurance_type FROM result_settlement_return {cond} ORDER BY profit_loss", params)
