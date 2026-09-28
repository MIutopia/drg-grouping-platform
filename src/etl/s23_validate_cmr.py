"""S23 Validate the 中治率 formula against the 6-7月 settlement returns.

The official return never carries the 中治率 itself, but it does carry the official group code:
a case settled as a TCM advantage group (F/R/Z suffix) has passed the platform's 中治率 gate
(>=60%). Recomputing the ratio from HIS fees and checking whether ~all such cases are >=60%
therefore validates the formula end-to-end - a wrong formula or subject mapping would show up
as many official-TCM cases below 60%.

This answers the 医保办 question raised by the 3-5月 back-test: are the handful of <60% cases a
formula problem, or an over-settlement? The answer so far is "a handful of genuine low-TCM-share
admissions (e.g. western traction), not a formula error" - and this job re-runs the check on
each new month so the answer stays current.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tcm_ratio import cmr_status, subject_totals  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

FILE = Path("原始资料/DRG结算样表/6、7月DRG结算明细.xlsx")


def load() -> pd.DataFrame:
    d6 = pd.read_excel(FILE, sheet_name="6月", dtype=str)
    d7 = pd.read_excel(FILE, sheet_name="7月", dtype=str)
    d = pd.concat([d6, d7], ignore_index=True)
    d = d[d["分组编码"].notna()].copy()
    d["病案号"] = d["病案号"].astype(str).str.strip()
    d["tcm"] = d["分组编码"].astype(str).str[-1].isin(["F", "R", "Z"])
    return d


def main() -> int:
    d = load()
    n = len(d)
    print(f"6-7月 有效病例 {n} 例 | 官方中医组(F/R/Z) {int(d['tcm'].sum())} 例")
    print("分组编码第4位分布:", dict(d["分组编码"].str[-1].value_counts()))

    ids = d["病案号"].dropna().tolist()
    in_clause = ",".join("'%s'" % x for x in ids)
    tot = subject_totals("sbo", "m.列(hospital_no) IN (%s)" % in_clause)
    cs = cmr_status(tot).reset_index()
    cs[src_col("hospital_no")] = cs[src_col("hospital_no")].astype(str).str.strip()

    m = d.merge(cs, left_on="病案号", right_on=src_col("hospital_no"), how="left")
    matched = int(m["zzl_passed"].notna().sum())
    print(f"与 HIS 费用可比对 {matched}/{n} 例（{matched / n:.1%}）")

    tcm = m[m["tcm"] & m["zzl_passed"].notna()]
    print(f"\n=== 官方中医组 {len(tcm)} 例（可比对）：重算中治率 ===")
    if len(tcm):
        ok = int(tcm["zzl_passed"].sum())
        print(f"  达标(>=60%) {ok} 例（{ok / len(tcm):.1%}）| 未达标 {len(tcm) - ok} 例")
        print(f"  中位数 {tcm['zzl_pct'].median():.1f}%  区间 "
              f"{tcm['zzl_pct'].min():.1f}% ~ {tcm['zzl_pct'].max():.1f}%")

    bad = tcm[~tcm["zzl_passed"]].sort_values("zzl_pct")
    print(f"\n=== 未达标的中医组病例（{len(bad)} 例）===")
    for r in bad.itertuples():
        print(f"  {r.病案号}  drg={r.分组编码}  中治率={r.zzl_pct:.1f}%  "
              f"总费用={r.住院医疗总费用}  结算日期={str(r.结算日期)[:10]}")

    # 非中医组对照
    ntcm = m[~m["tcm"] & m["zzl_passed"].notna()]
    if len(ntcm):
        print(f"\n=== 官方非中医组 {len(ntcm)} 例对照 ===")
        print(f"  达标 {int(ntcm['zzl_passed'].sum())} 例"
              f"（{ntcm['zzl_passed'].mean():.1%}）| 中位数 {ntcm['zzl_pct'].median():.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
