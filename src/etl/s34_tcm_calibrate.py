"""P0: calibrate the TCM-group trigger against the official returns.

The 76 cases where the engine enters a TCM advantage group but the platform billed a
standard group are the single largest slice of the 4-digit gap (docs/32). This job
replays all N cases through the engine and grid-searches a tighter global gate
(min inpatient days x min number of TCM operation classes) on top of the existing
per-group rules, scoring every candidate against the official DRG code.

Baseline (no extra gate) is scored first and must reproduce result_compare_case.grp_engine,
otherwise the input assembly differs from S11 and every delta below is worthless.

Read-only; production config is never modified. Docs: docs/32-四位码一致率根因与反事实验证.md §3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))

from dbio import query_df  # noqa: E402
from drg_engine import DrgEngine  # noqa: E402
from settings import DICT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "coding"
OUT_SUB.mkdir(parents=True, exist_ok=True)
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"

DAY_GRID = [0, 5, 8, 10, 12, 14, 20]
CLASS_GRID = [0, 2, 3]
CMR_GRID = [0, 0.6]               # 中治率门槛；0 = 不启用（现状）


def _maps() -> tuple[dict[str, str], dict[str, str]]:
    m = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    m.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    return (
        {str(r.country).strip(): str(r.yb).strip()
         for r in m[m["kind"] == "S"].itertuples() if r.yb},
        {str(r.country).strip(): str(r.yb).strip()
         for r in m[m["kind"] == "D"].itertuples() if r.yb},
    )


def _quoted(nos: list[str]) -> str:
    return ",".join("'" + str(n).replace("'", "''") + "'" for n in nos)


def build(eng: DrgEngine) -> pd.DataFrame:
    base = query_df(
        "SELECT medical_no, grp_official, grp_engine, adrg_official, adrg_engine, "
        "path, actual_days, main_diag_code, main_op_code FROM drg.dbo.result_compare_case")
    base["medical_no"] = base["medical_no"].astype(str).str.strip()
    nos = base["medical_no"].tolist()
    q = _quoted(nos)
    code_map, dx_map = _maps()

    dxs = query_df(
        "SELECT 列(hospital_no), 列(order), 列(icd) FROM 源表(diagnosis) "
        f"WHERE 列(hospital_no) IN ({q}) AND 列(kind_input) IN (3, 4) AND 列(kind_diagnose) = 2 "
        "ORDER BY 列(hospital_no), 列(order), 列(icd)")
    main_dx: dict[str, str] = {}
    other_dx: dict[str, set[str]] = {}
    for r in dxs.itertuples():
        yb = dx_map.get(str(getattr(r, src_col("icd"))).strip(), str(getattr(r, src_col("icd"))).strip())
        if int(getattr(r, src_col("order")) or 0) == 1:
            main_dx.setdefault(getattr(r, src_col("hospital_no")), yb)
        else:
            other_dx.setdefault(getattr(r, src_col("hospital_no")), set()).add(yb)

    # Fallback used by S11: admissions with no discharge main diagnosis fall back to the
    # vendor pre-grouping table. Omitting it leaves those cases ungroupable here and
    # silently shrinks the replay.
    fb = query_df(
        "SELECT m.列(hospital_no), g.列(icd_yb) FROM 源表(admission) m "
        "JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = m.列(serial) "
        f"WHERE m.列(hospital_no) IN ({q}) AND g.列(icd_yb) IS NOT NULL")
    fb = fb.drop_duplicates(src_col("hospital_no"))
    for r in fb.itertuples():
        if getattr(r, src_col("hospital_no")) not in main_dx:
            main_dx[getattr(r, src_col("hospital_no"))] = str(getattr(r, src_col("icd_yb"))).strip()

    ops = query_df(
        "SELECT m.列(hospital_no), o.列(code) FROM 源表(operation) o "
        f"JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) WHERE m.列(hospital_no) IN ({q})")
    ops_by_case: dict[str, set[str]] = {}
    for r in ops.itertuples():
        yb = code_map.get(str(getattr(r, src_col("code"))).strip(), str(getattr(r, src_col("code"))).strip())
        ops_by_case.setdefault(getattr(r, src_col("hospital_no")), set()).add(yb)

    # The settlement return carries actual_days for only ~22% of cases; 源表(admission) always has
    # it and matches the official value. Use HIS first so the days gate actually applies.
    his_days = query_df(
        f"SELECT 列(hospital_no), 列(inhospital_day) FROM 源表(admission) WHERE 列(hospital_no) IN ({q})")
    his_days_map = {str(getattr(r, src_col("hospital_no"))): pd.to_numeric(getattr(r, src_col("inhospital_day")), errors="coerce")
                    for r in his_days.itertuples()}

    from tcm_ratio import executed_op_classes  # noqa: PLC0415
    adv = executed_op_classes("sbo", f"m.列(hospital_no) IN ({q})")
    cls_by_case: dict[str, list] = {}
    if not adv.empty:
        for r in adv.itertuples():
            cls_by_case[str(getattr(r, src_col("hospital_no")))] = list(r.op_classes)

    from tcm_ratio import cmr_status, subject_totals  # noqa: PLC0415
    cmr = cmr_status(subject_totals("sbo", f"m.列(hospital_no) IN ({q})"))
    cmr_pct = {str(i): cmr.loc[i, "zzl_pct"] for i in cmr.index}
    cmr_ok = {str(i): bool(cmr.loc[i, "zzl_passed"]) for i in cmr.index}

    rows = []
    for r in base.itertuples():
        hs = r.medical_no
        days = his_days_map.get(hs)
        if days is None or pd.isna(days):
            days = pd.to_numeric(r.actual_days, errors="coerce")
        classes = set(cls_by_case.get(hs, []) or [])
        mdx = main_dx.get(hs) or (str(r.main_diag_code).strip() or None)
        tcm_res = eng.tcm.group(mdx, classes, days) if eng.tcm else {"drg": None}
        std_res = eng.std.group(mdx, ops_by_case.get(hs, set()), other_dx.get(hs, set()))
        rows.append({
            "住院号": hs,
            "官方码": r.grp_official,
            "表内基线码": r.grp_engine,
            "中医组未加闸": tcm_res.get("drg"),
            "标准路径码": std_res.get("drg"),
            "住院天数": days,
            "中医操作类数": len(classes),
            "操作类": "|".join(sorted(classes)),
            "中治率": cmr_pct.get(hs),
            "中治率达标": cmr_ok.get(hs),
        })
    return pd.DataFrame(rows)


def _blank(v) -> bool:
    """NaN is truthy in Python, so `if t:` would treat a miss as a hit - test explicitly."""
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    return str(v).strip() in ("", "nan", "None", "<NA>")


def score(df: pd.DataFrame, d: int, k: int, c: float) -> dict:
    def pick(r):
        t = r["中医组未加闸"]
        if _blank(t):
            return r["标准路径码"]
        if d > 0 and not (pd.notna(r["住院天数"]) and r["住院天数"] >= d):
            return r["标准路径码"]
        if k > 0 and r["中医操作类数"] < k:
            return r["标准路径码"]
        if c > 0:
            z = pd.to_numeric(r["中治率"], errors="coerce")
            if pd.isna(z) or (z / 100.0) < c:
                return r["标准路径码"]
        return t
    pred = df.apply(pick, axis=1)
    off = df["官方码"].map(lambda v: "" if _blank(v) else str(v).strip())
    p = pred.map(lambda v: "" if _blank(v) else str(v).strip())
    agree = int((p == off).sum())
    # ADRG-level agreement (first three chars)
    adrg_ok = int((p.str[:3] == off.str[:3]).sum())
    off_tcm = ~off.str[-1].str.isdigit()
    pred_tcm = ~p.str[-1].str.isdigit()
    return {"天数": d, "操作类": k, "中治率门槛": c, "四位码一致": agree,
            "一致率": agree / len(df), "ADRG一致率": adrg_ok / len(df),
            "误入中医组": int((pred_tcm & ~off_tcm).sum()),
            "漏入中医组": int((~pred_tcm & off_tcm).sum())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12, help="rows of the grid table to print")
    ap.add_argument("--save", action="store_true", help="persist the per-case replay")
    a = ap.parse_args()

    eng = DrgEngine()
    if not eng.ready:
        print("- 引擎字典未就绪")
        return 1

    df = build(eng)
    base_ok = int((df["表内基线码"].astype(str).str.strip()
                   == df["中医组未加闸"].combine_first(df["标准路径码"]).astype(str).str.strip()).sum())
    days_null = int(pd.to_numeric(df["住院天数"], errors="coerce").isna().sum())
    print(f"## 回放 {len(df)} 例")
    print(f"- 基线复现（未加闸预测 vs 表内 grp_engine）：**{base_ok}/{len(df)}**"
          f"（{base_ok / len(df):.1%}）——偏低则入参装配与 S11 不一致，下表不可信")
    print(f"- **住院天数缺失：{days_null} 例（{days_null / len(df):.1%}）**"
          f"——NaN 参与 `days < 门槛` 比较恒为 False，天数门槛被绕过（见 docs/21 同类缺陷）\n")

    rows = [score(df, d, k, c) for d in DAY_GRID for k in CLASS_GRID for c in CMR_GRID]
    res = pd.DataFrame(rows).sort_values("四位码一致", ascending=False).reset_index(drop=True)
    cur = res[(res["天数"] == 0) & (res["操作类"] == 0) & (res["中治率门槛"] == 0)].iloc[0]
    print("### 网格搜索（按四位码一致排序）")
    print(res.head(a.top).to_string(index=False))
    print(f"\n- 现状（不额外加闸）：四位码一致 **{int(cur['四位码一致'])}**（{cur['一致率']:.1%}），"
          f"误入中医组 **{int(cur['误入中医组'])}**，漏入中医组 **{int(cur['漏入中医组'])}**")
    best = res.iloc[0]
    print(f"- 最优：天数 ≥ {int(best['天数'])}、操作类 ≥ {int(best['操作类'])}、"
          f"中治率 ≥ {best['中治率门槛']:.0%}"
          f" → 四位码一致 **{int(best['四位码一致'])}**（{best['一致率']:.1%}），"
          f"误入 {int(best['误入中医组'])}，漏入 {int(best['漏入中医组'])}")

    if a.save:
        df.to_csv(OUT_SUB / "s34_tcm_replay.csv", index=False, encoding="utf-8-sig")
        res.to_csv(OUT_SUB / "s34_tcm_grid.csv", index=False, encoding="utf-8-sig")
        print(f"\n> 已写入 {OUT_SUB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
