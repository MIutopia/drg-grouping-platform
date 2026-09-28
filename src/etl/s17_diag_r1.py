"""S17 Diagnose why R1's precision stays low after the homepage code mapping.

R1 fires when the fees support a TCM group that the submitted codes do not reach. After
wiring in the homepage code map the alert count only fell from 450 to 394, so the remaining
false positives must come from somewhere else. This job looks at the official outcome of the
alerted cases and at a few concrete examples, to decide whether the rule can be fixed or has
to be re-scoped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import s14_coding_assistant as s14  # noqa: E402
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402

OUT_SUB = OUT_DIR / "coding"
TCM_SUFFIX = ("F", "R", "Z")


def main() -> int:
    findings = pd.read_csv(OUT_SUB / "coding_findings.csv", dtype=str, encoding="utf-8-sig")
    off = pd.read_csv(s14.SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    off["k"] = off["medical_no"].astype(str).str.strip()
    off_map = dict(zip(off["k"], off["drg_code"].astype(str).str.strip()))
    day_map = dict(zip(off["k"], pd.to_numeric(off["actual_days"], errors="coerce")))

    r1 = set(findings.loc[findings["rule"] == "R1", "medical_no"].astype(str))
    lost = pd.read_csv(OUT_SUB / "counterfactual_tcm_loss.csv", dtype=str, encoding="utf-8-sig")
    confirmed = set(lost["medical_no"].astype(str))

    rows = []
    for h in sorted(r1):
        off_drg = off_map.get(h, "")
        rows.append({"medical_no": h, "official_drg": off_drg,
                     "official_is_tcm": off_drg.endswith(TCM_SUFFIX),
                     "confirmed": h in confirmed,
                     "days_known": pd.notna(day_map.get(h))})
    d = pd.DataFrame(rows)

    print(f"R1 触发 {len(d)} 例")
    print(f"  其中经官方结果确证（③）: {int(d['confirmed'].sum())}")
    print(f"  官方组码也是中医组（F/R/Z）: {int(d['official_is_tcm'].sum())}"
          f"（{d['official_is_tcm'].mean():.1%}）")
    print(f"  官方组码非中医组: {int((~d['official_is_tcm']).sum())}")
    print()
    print("R1 触发病例的官方组码分布（Top 12）:")
    print(d["official_drg"].value_counts().head(12).to_string())
    print()
    print("住院天数是否已知（官方返回仅 3 月表带天数）:")
    print(d["days_known"].value_counts().to_string())
    print()

    # concrete example: an alert where the official result is already a TCM group
    ex = d[d["official_is_tcm"] & ~d["confirmed"]].head(3)["medical_no"].tolist()
    if ex:
        print(f"=== 典型案例（官方已是中医组但仍触发 R1）: {ex} ===")
        op_map, dx_map = s14.load_code_map()
        g = s14.build_groups(s14.load_cases(), s14.load_op_class_map())
        for h in ex:
            print(f"\n--- {h} ---")
            print(f"  主诊断: {g['main_dx'].get(h)}  官方组: {off_map.get(h)}"
                  f"  官方天数: {day_map.get(h)}")
            print(f"  费用侧操作类: {sorted(g['tcm_cls'].get(h, []))}")
            print(f"  首页码可证明类: {sorted(g['home_cls'].get(h, set()))}")
            rows_h = g["ops"].get(h)
            print(f"  HIS 操作码: {sorted(set(rows_h['code'])) if rows_h is not None else '无'}")
    d.to_csv(OUT_SUB / "s17_r1_diagnosis.csv", index=False, encoding="utf-8-sig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
