"""Counterfactual: would adding the missed other-diagnoses found by S32 improve grouping?

Two parts:
  A. counterfactual - re-run the engine for the flagged cases with the candidate codes
     added to 其他诊断, and compare against the platform's official DRG code. The
     baseline prediction is re-computed here (not read from the table) and validated
     against result_compare_case.grp_engine first, so a broken input assembly cannot
     silently produce a fake delta.
  B. root cause - decompose the whole N-case gap into ADRG mismatch vs. severity
     (CC/MCC) mismatch, and show which direction the severity errors go.

Read-only. Docs: docs/32-四位码一致率根因与反事实验证.md
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
from settings import CONFIG_DIR, DICT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "coding"
OUT_SUB.mkdir(parents=True, exist_ok=True)
CLUE_CSV = OUT_SUB / "s32_dx_audit.csv"
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"


def _codes_map() -> tuple[dict[str, str], dict[str, str]]:
    m = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    m.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    return (
        {str(r.country).strip(): str(r.yb).strip()
         for r in m[m["kind"] == "S"].itertuples() if r.yb},
        {str(r.country).strip(): str(r.yb).strip()
         for r in m[m["kind"] == "D"].itertuples() if r.yb},
    )


def _in(n: int) -> str:
    return ",".join(["%s"] * n)


def load_inputs(nos: list[str]) -> tuple[dict[str, str], dict[str, set[str]],
                                         dict[str, set[str]], dict[str, list],
                                         dict[str, str], dict[str, str]]:
    """Rebuild exactly the inputs s11_adrg.py feeds to the engine."""
    ph = _in(len(nos))
    code_map, dx_map = _codes_map()

    dxs = query_df(
        "SELECT 列(hospital_no), 列(order), 列(icd) FROM 源表(diagnosis) "
        f"WHERE 列(hospital_no) IN ({ph}) AND 列(kind_input) IN (3, 4) "
        "AND 列(kind_diagnose) = 2 ORDER BY 列(hospital_no), 列(order), 列(icd)", params=nos)
    main_dx: dict[str, str] = {}
    other_dx: dict[str, set[str]] = {}
    for r in dxs.itertuples():
        yb = dx_map.get(str(getattr(r, src_col("icd"))).strip(), str(getattr(r, src_col("icd"))).strip())
        if int(getattr(r, src_col("order")) or 0) == 1:
            main_dx.setdefault(getattr(r, src_col("hospital_no")), yb)
        else:
            other_dx.setdefault(getattr(r, src_col("hospital_no")), set()).add(yb)

    ops = query_df(
        "SELECT m.列(hospital_no), o.列(code) FROM 源表(operation) o "
        "JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) "
        f"WHERE m.列(hospital_no) IN ({ph})", params=nos)
    ops_by_case: dict[str, set[str]] = {}
    for r in ops.itertuples():
        yb = code_map.get(str(getattr(r, src_col("code"))).strip(), str(getattr(r, src_col("code"))).strip())
        ops_by_case.setdefault(getattr(r, src_col("hospital_no")), set()).add(yb)

    from tcm_ratio import executed_op_classes  # noqa: PLC0415
    quoted = ",".join("'" + str(n).replace("'", "''") + "'" for n in nos)
    adv = executed_op_classes("sbo", f"m.列(hospital_no) IN ({quoted})")
    cls_by_case: dict[str, list] = {}
    if not adv.empty:
        for r in adv.itertuples():
            cls_by_case[str(getattr(r, src_col("hospital_no")))] = list(r.op_classes)
    return main_dx, other_dx, ops_by_case, cls_by_case, dx_map, code_map


def part_a(eng: DrgEngine) -> pd.DataFrame | None:
    """Counterfactual on the flagged cases."""
    if not CLUE_CSV.exists():
        print("- 缺少 S32 线索文件，请先运行 s32_dx_audit.py --audit")
        return None
    clue = pd.read_csv(CLUE_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    if clue.empty:
        print("- 线索为空")
        return None
    cand: dict[str, set[str]] = {}
    for r in clue.itertuples():
        cand.setdefault(str(r.住院号), set()).add(str(r.候选编码).strip())

    nos = sorted(cand.keys())
    ph = _in(len(nos))
    base_tbl = query_df(
        "SELECT medical_no, grp_official, grp_engine, adrg_official, adrg_engine, "
        f"agree_drg, agree_adrg, path, actual_days FROM drg.dbo.result_compare_case "
        f"WHERE medical_no IN ({ph})", params=nos)
    days = {str(r.medical_no): r.actual_days for r in base_tbl.itertuples()}

    main_dx, other_dx, ops_by_case, cls_by_case, dx_map, _ = load_inputs(nos)

    rows, ok_repro = [], 0
    for r in base_tbl.itertuples():
        hs = str(r.medical_no)
        add = {dx_map.get(c, c) for c in cand.get(hs, set())}
        common = dict(main_dx=main_dx.get(hs), ops=ops_by_case.get(hs, set()),
                      tcm_classes=set(cls_by_case.get(hs, []) or []),
                      days=pd.to_numeric(days.get(hs), errors="coerce"))
        b = eng.group(other_dx=other_dx.get(hs, set()), **common)
        n = eng.group(other_dx=set(other_dx.get(hs, set())) | add, **common)
        if str(b.get("drg") or "") == str(r.grp_engine or ""):
            ok_repro += 1
        rows.append({
            "住院号": hs, "官方码": r.grp_official, "基线引擎码": b.get("drg"),
            "表内基线码": r.grp_engine, "补入后码": n.get("drg"),
            "补入候选": "|".join(sorted(add)), "基线一致": int(str(r.grp_official) == str(b.get("drg"))),
            "补入后一致": int(str(r.grp_official) == str(n.get("drg"))),
            "是否变化": int(str(b.get("drg")) != str(n.get("drg"))),
        })
    df = pd.DataFrame(rows)
    print(f"## A. 反事实（{len(df)} 例）")
    print(f"- 基线复现校验：{ok_repro}/{len(df)} 与表内 grp_engine 一致"
          f"（低则说明入参装配与 S11 不同，结论不可信）")
    if len(df):
        ch = df[df["是否变化"] == 1]
        print(f"- 补入候选后**分组发生变化**：{len(ch)} 例")
        print(f"- 四位码一致：基线 {int(df['基线一致'].sum())} → 补入后 {int(df['补入后一致'].sum())}")
        if len(ch):
            print("\n### 发生变化的病例")
            print(ch[["住院号", "官方码", "基线引擎码", "补入后码", "补入候选"]].to_string(index=False))
        imp = df[(df["基线一致"] == 0) & (df["补入后一致"] == 1)]
        worse = df[(df["基线一致"] == 1) & (df["补入后一致"] == 0)]
        print(f"\n- 变好（不一致→一致）：**{len(imp)}** 例；变坏：**{len(worse)}** 例")
        if len(imp):
            print(imp[["住院号", "官方码", "基线引擎码", "补入后码"]].to_string(index=False))
    return df


def part_b() -> pd.DataFrame:
    """Root-cause decomposition over the whole comparison table."""
    df = query_df(
        "SELECT medical_no, grp_official, grp_engine, adrg_official, adrg_engine, "
        "agree_drg, agree_adrg, path, reason FROM drg.dbo.result_compare_case")
    n = len(df)
    df["agree_drg"] = pd.to_numeric(df["agree_drg"], errors="coerce").fillna(0).astype(int)
    df["agree_adrg"] = pd.to_numeric(df["agree_adrg"], errors="coerce").fillna(0).astype(int)

    sev_only = df[(df["agree_adrg"] == 1) & (df["agree_drg"] == 0)]
    adrg_bad = df[df["agree_adrg"] == 0]
    print(f"\n## B. 根因分解（{n} 例）")
    print(f"- 四位码一致 {int(df['agree_drg'].sum())}（{df['agree_drg'].mean():.1%}）"
          f"；ADRG 一致 {int(df['agree_adrg'].sum())}（{df['agree_adrg'].mean():.1%}）")
    print(f"- **ADRG 相同但档位不同（CC/MCC 判定差异）**：{len(sev_only)} 例"
          f"（占全部 {len(sev_only) / n:.1%}）")
    print(f"- **ADRG 就不同（主诊断/手术问题）**：{len(adrg_bad)} 例"
          f"（占全部 {len(adrg_bad) / n:.1%}）")

    # direction of the severity error, only where both codes are 4 chars
    def sev(x):
        s = str(x or "").strip()
        return s[-1] if len(s) == 4 else ""
    sev_only = sev_only.copy()
    sev_only["官方档"] = sev_only["grp_official"].map(sev)
    sev_only["引擎档"] = sev_only["grp_engine"].map(sev)
    dirs = sev_only.groupby(["引擎档", "官方档"]).size().sort_values(ascending=False)
    print("\n### 档位差异方向（引擎档 → 官方档，前 10）")
    print(dirs.head(10).to_string())

    print("\n### 未入组/异常原因分布（reason）")
    print(df["reason"].fillna("(空)").value_counts().head(8).to_string())
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["a", "b", "all"], default="all")
    a = ap.parse_args()

    df_a = df_b = None
    if a.part in ("b", "all"):
        df_b = part_b()
    if a.part in ("a", "all"):
        eng = DrgEngine()
        if not eng.ready:
            print("- 引擎字典未就绪")
            return 1
        df_a = part_a(eng)

    if df_a is not None and len(df_a):
        df_a.to_csv(OUT_SUB / "s33_counterfactual.csv", index=False, encoding="utf-8-sig")
        print(f"\n> 已写入 {OUT_SUB / 's33_counterfactual.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
