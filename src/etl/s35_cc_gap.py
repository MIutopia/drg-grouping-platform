"""P1: why does the engine under-call "伴合并症或并发症" (digit 5 vs official 3)?

For the cases where the ADRG agrees but the engine returns the no-complication variant
while the platform billed the with-complication one, split the cause into:
  A. the comorbidity code is simply absent from 源表(diagnosis) (never coded);
  B. it is present but sits in the CHS-DRG exclusion table, so it never counts;
  C. it is present and is a genuine CC/MCC but our CC/MCC table does not list it;
  D. the platform received other-diagnosis codes that HIS does not carry at all
     (upload source richer than HIS - compare against the settlement return echo).

Read-only. Docs: docs/32-四位码一致率根因与反事实验证.md §4
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))

from dbio import query_df  # noqa: E402
from drg_engine import DrgEngine, norm  # noqa: E402
from settings import DICT_DIR  # noqa: E402

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "coding"
REPLAY = OUT_SUB / "s34_tcm_replay.csv"
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    return str(v).strip() in ("", "nan", "None", "<NA>")


def _quoted(nos: list[str]) -> str:
    return ",".join("'" + str(n).replace("'", "''") + "'" for n in nos)


def pick_cases() -> list[str]:
    d = pd.read_csv(REPLAY, dtype=str, encoding="utf-8-sig").fillna("")
    pred = d["中医组未加闸"].where(d["中医组未加闸"] != "", d["标准路径码"])
    off = d["官方码"]
    sel = (pred.str[-1] == "5") & (off.str[-1] == "3") & (pred.str[:3] == off.str[:3])
    return d.loc[sel, "住院号"].tolist()


def main() -> int:
    if not REPLAY.exists():
        print("- 缺少 s34_tcm_replay.csv，请先运行 s34_tcm_calibrate.py --save")
        return 1
    nos = pick_cases()
    print(f"## P1 诊断：引擎判「不伴(5)」、官方判「伴合并症(3)」共 **{len(nos)}** 例\n")
    if not nos:
        return 0

    eng = DrgEngine()
    if not eng.ready:
        print("- 引擎字典未就绪")
        return 1
    cc, mcc, excl = eng.std.cc, eng.std.mcc, eng.std.exclude

    m = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    m.columns = ["d", "country", "cn", "yb", "ybn", "kind", "old"]
    dx_map = {str(r.country).strip(): str(r.yb).strip()
              for r in m[m["kind"] == "D"].itertuples() if r.yb}

    q = _quoted(nos)
    dxs = query_df(
        "SELECT 列(hospital_no), 列(icd) FROM 源表(diagnosis) "
        f"WHERE 列(hospital_no) IN ({q}) AND 列(kind_input) IN (3, 4) "
        "AND 列(kind_diagnose) = 2 AND 列(order) > 1")
    his: dict[str, set[str]] = {}
    for r in dxs.itertuples():
        his.setdefault(str(getattr(r, src_col("hospital_no"))), set()).add(
            norm(dx_map.get(str(getattr(r, src_col("icd"))).strip(), str(getattr(r, src_col("icd"))).strip())))

    ret = query_df(
        "SELECT medical_no, other_diag_codes FROM drg.dbo.result_settlement_return "
        f"WHERE medical_no IN ({q})")
    plat: dict[str, set[str]] = {}
    for r in ret.itertuples():
        raw = "" if _blank(r.other_diag_codes) else str(r.other_diag_codes)
        plat[str(r.medical_no).strip()] = {norm(c) for c in raw.split("|") if c.strip()}

    bucket = {"A_未录入(平台也无)": 0, "B_被排除表挡掉": 0,
              "C_CC表未收录": 0, "D_平台比HIS多码": 0, "E_已有CC仍判5(其他)": 0}
    rows = []
    for hs in nos:
        h = his.get(hs, set())
        p = plat.get(hs, set())
        h_cc = (h - excl) & cc
        h_mcc = (h - excl) & mcc
        extra = p - h                       # platform has, HIS does not
        extra_cc = (extra - excl) & (cc | mcc)
        new_cc = ((h - excl) & (cc | mcc)) == set() and bool(h - excl)

        if h_mcc or h_cc:
            tag = "E_已有CC仍判5(其他)"
        elif extra_cc:
            tag = "D_平台比HIS多码"
        elif (h & excl):
            tag = "B_被排除表挡掉"
        elif h:
            tag = "C_CC表未收录"
        else:
            tag = "A_未录入(平台也无)" if not p else "D_平台比HIS多码"
        bucket[tag] += 1
        rows.append({"住院号": hs, "HIS其他诊断数": len(h), "平台回传诊断数": len(p),
                     "平台独有": len(extra), "平台独有含CC": len(extra_cc),
                     "HIS命中CC": len(h_cc), "HIS命中MCC": len(h_mcc),
                     "HIS被排除": len(h & excl), "归类": tag})

    df = pd.DataFrame(rows)
    print("### 归类统计")
    for k, v in sorted(bucket.items(), key=lambda kv: -kv[1]):
        print(f"- {k}：**{v}** 例（{v / len(nos):.1%}）")
    print()
    print("### 平台回传覆盖情况")
    print(f"- 有平台回传 other_diag_codes 的：**{int((df['平台回传诊断数'] > 0).sum())}/{len(df)}** 例")
    print(f"- 平台独有编码数 > 0 的：**{int((df['平台独有'] > 0).sum())}** 例")
    print(f"- 平台独有编码中含 CC/MCC 的：**{int((df['平台独有含CC'] > 0).sum())}** 例")
    print()
    print("### 明细（前 15 例）")
    print(df.head(15).to_string(index=False))

    df.to_csv(OUT_SUB / "s35_cc_gap.csv", index=False, encoding="utf-8-sig")
    print(f"\n> 已写入 {OUT_SUB / 's35_cc_gap.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
