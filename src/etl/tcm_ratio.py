"""Shared helpers for the TCM treatment cost ratio (中治率) and for deriving TCM
operation classes from the advice table.

The ratio is a hard settlement condition under 某地区医保局〔2025〕27号: cases below 60% are
settled at the plain medical ADRG weight instead of the TCM advantage group weight.
Docs: docs/16-P0数据源打通与科目映射.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

sys.path.insert(0, str(Path(__file__).resolve().parent))

CONFIG_DICT = Path(__file__).resolve().parents[1] / "config" / "dict"
ATTR_CSV = CONFIG_DICT / "cost_item_attribute.csv"

SUBJECT_TCM_TREAT = "中医治疗费"
SUBJECT_WEST_TREAT = "西医治疗费"
SUBJECT_TCM_PATENT = "中成药费"
SUBJECT_TCM_HERBAL = "中药饮片费"
SUBJECT_WEST_DRUG = "西药费"
SUBJECT_SURGERY = "手术费"
SUBJECT_MATERIAL = "卫生材料费"

# Numerator = 中医治疗费 + 中成药费 + 中药饮片费
NUMERATOR = (SUBJECT_TCM_TREAT, SUBJECT_TCM_PATENT, SUBJECT_TCM_HERBAL)
# Denominator adds western treatment, western drugs, surgery and consumables
DENOMINATOR = NUMERATOR + (SUBJECT_WEST_TREAT, SUBJECT_WEST_DRUG, SUBJECT_SURGERY, SUBJECT_MATERIAL)

# Medicine fee sort codes (see S09 report)
MEDICINE_SORT_SUBJECT = {"01": SUBJECT_WEST_DRUG, "02": SUBJECT_TCM_PATENT,
                         "03": SUBJECT_TCM_HERBAL, "13": SUBJECT_MATERIAL}


def load_cost_attr() -> pd.DataFrame:
    """Charge-item attribute dictionary built by S09."""
    a = pd.read_csv(ATTR_CSV, dtype=str, encoding="utf-8-sig")
    a[src_col("cost_no")] = a[src_col("cost_no")].astype(str).str.strip()
    a["op_class"] = a["op_class"].fillna("")
    a["zzl_subject"] = a["zzl_subject"].fillna("其他")
    return a.drop_duplicates(src_col("cost_no"))


def subject_totals(profile: str, patient_filter: str) -> pd.DataFrame:
    """Sum fees per admission and per 中治率 subject.

    patient_filter is a SQL fragment joining 源表(admission) m, e.g. "m.列(mark_out) = 0".
    """
    from dbio import query_df  # noqa: PLC0415

    sql = (
        "SELECT m.列(hospital_no), t.src, t.列(sort_code), t.列(cost_no), SUM(t.列(money)) AS money FROM ("
        "  SELECT f.列(hospital_no), 'cure' AS src, f.列(sort_code), f.列(cost_no), f.列(money) "
        "    FROM 源表(fee_cure_in) f WHERE f.列(mark_dell) = 0 "
        "  UNION ALL SELECT f.列(hospital_no), 'check', f.列(sort_code), f.列(cost_no), f.列(money) "
        "    FROM 源表(fee_check_in) f WHERE f.列(mark_dell) = 0 "
        "  UNION ALL SELECT f.列(hospital_no), 'medicine', f.列(sort_code), f.列(cost_no), f.列(money) "
        "    FROM 源表(fee_medicine_in) f WHERE f.列(mark_dell) = 0 "
        ") t JOIN 源表(admission) m ON m.列(hospital_no) = t.列(hospital_no) "
        f"WHERE {patient_filter} GROUP BY m.列(hospital_no), t.src, t.列(sort_code), t.列(cost_no)")

    df = query_df(sql)
    if df.empty:
        return df

    attr = load_cost_attr()
    code_to_subject = dict(zip(attr[src_col("cost_no")], attr["zzl_subject"]))
    df["subject"] = df.apply(
        lambda r: (MEDICINE_SORT_SUBJECT.get(str(r[src_col("sort_code")]).strip(), "其他")
                   if r["src"] == "medicine"
                   else code_to_subject.get(str(r[src_col("cost_no")]).strip(), "其他")),
        axis=1)
    return df.groupby([src_col("hospital_no"), "subject"])["money"].sum().unstack(fill_value=0.0)


def ratio(totals: pd.DataFrame) -> pd.DataFrame:
    """Add numerator / denominator / ratio columns to a subject-total frame."""
    out = totals.copy()
    for col in DENOMINATOR:
        if col not in out.columns:
            out[col] = 0.0
    num = out[list(NUMERATOR)].sum(axis=1)
    den = out[list(DENOMINATOR)].sum(axis=1)
    out["zzl_numerator"] = num.round(2)
    out["zzl_denominator"] = den.round(2)
    out["zzl_ratio"] = (num / den).where(den > 0).round(4)
    return out


CMR_THRESHOLD = 0.60


def cmr_status(totals: pd.DataFrame) -> pd.DataFrame:
    """Per-admission 中治率 verdict, comparing the RAW ratio against 60% (no pre-rounding).

    The reference calculator rounds to one decimal percent and only then compares to 60.0,
    which would let a case at 59.95% pass (it rounds up to 60.0). The policy reads 中治率 ≥60%,
    so the raw ratio is the correct comparison; rounding is for display only. On the N-case
    cohort the two conventions happen to agree (no case falls in the rounding band, verified
    2026-09-20), but the stricter reading is the safe one should a case ever land on the
    boundary at year-end settlement - a wrongly-promoted TCM group would be clawed back.
    """
    r = ratio(totals)
    num = r[list(NUMERATOR)].sum(axis=1)
    den = r[list(DENOMINATOR)].sum(axis=1)
    raw = (num / den).where(den > 0)
    out = pd.DataFrame(index=r.index)
    out["zzl_raw"] = raw
    out["zzl_pct"] = (raw * 100).round(1)          # display only
    out["zzl_passed"] = raw >= CMR_THRESHOLD       # verdict on the raw value
    out["zzl_gap_pct"] = ((CMR_THRESHOLD - raw) * 100).where(raw < CMR_THRESHOLD).round(1)
    return out


def executed_op_classes(profile: str, patient_filter: str) -> pd.DataFrame:
    """TCM operation classes per admission, derived from the advice table.

    Only orders with 列(time_execute) set count as executed. Class ids follow
    27号文: 0 bone-setting, 1 tuina, 2 needling, 3 moxibustion, 4 dressing/fumigation.
    """
    from dbio import query_df  # noqa: PLC0415

    sql = (
        "SELECT m.列(hospital_no), a.列(cost_no), a.列(cost_name), a.列(time_execute) FROM ("
        "  SELECT 列(hospital_no), 列(cost_no), 列(cost_note) AS 列(cost_name), 列(time_execute) "
        "    FROM 源表(advice_long) "
        "  UNION ALL SELECT 列(hospital_no), 列(cost_no), 列(cost_note), 列(time_execute) "
        "    FROM 源表(advice_temp) "
        ") a JOIN 源表(admission) m ON m.列(hospital_no) = a.列(hospital_no) "
        f"WHERE {patient_filter}")

    df = query_df(sql)
    if df.empty:
        return pd.DataFrame(columns=[src_col("hospital_no"), "op_classes", "op_names"])

    attr = load_cost_attr()
    code_to_class = dict(zip(attr[src_col("cost_no")], attr["op_class"]))
    df["op_class"] = df[src_col("cost_no")].astype(str).str.strip().map(code_to_class).fillna("")
    ex = df[(df["op_class"] != "") & df[src_col("time_execute")].notna()]

    grp = ex.groupby(src_col("hospital_no"))
    return pd.DataFrame({
        "op_classes": grp["op_class"].apply(lambda s: sorted(set(s.astype(str)))),
        "op_names": grp[src_col("cost_name")].apply(lambda s: "、".join(sorted(set(s.astype(str))))),
    }).reset_index()
