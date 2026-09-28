"""S02 Difference analysis: official settlement returns vs vendor pre-grouping.

Baseline quantification before the self-built engine goes live.
Docs: docs/15-代码模块说明与作业手册.md §6.3.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402

SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"
OUT_SUB = OUT_DIR / "compare"

# 回测窗口按导入的结算返回自适应（出院时间）。此前写死 2026-02-20 ~ 2026-06-01
# 只覆盖 3~5 月，新来 6/7 月返回后若不改代码，新月份会被整段挡在窗口外。
from window_util import OUT_FALLBACK_TO, OUT_FROM_DEFAULT, window_from  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

# 列名是动态拼接的（列(money_sortcode_)01..21），所以拼进 SQL 的仍是令牌：dbio 执行前把
# 列(money_sortcode_) 展开成物理列名，序号后缀保持不动。
SORTCODES = ", ".join(f"g.列(money_sortcode_){i:02d}" for i in range(1, 22))

PRELIM_SQL = """
SELECT h.列(serial), h.列(hospital_no), h.列(ic_no), h.列(office_now), h.列(office_out),
       h.列(doctor_residency),
       h.列(time_enter), h.列(time_out), h.列(inhospital_day),
       h.列(money_all), h.列(money_own), h.列(money_medicare), h.n_DRGS,
       h.列(ic_id),
       g.n_id AS g_id, g.列(group_code), g.列(group_name), g.列(rate), g.列(basic_rate),
       g.列(grade_coefficient), g.列(standard_cost), g.列(case_type),
       g.列(icd_gm), g.列(note_gm), g.列(icd_yb), g.列(note_yb),
       g.列(operation_code_gm), g.列(operation_name_gm),
       g.列(operation_code_yb), g.列(operation_name_yb),
       g.列(magnification), g.列(time_rebuild), g.列(kind), g.列(mark_check),
       {sortcodes},
       g.列(mark_cn), g.列(reason_cn), g.列(coefficient_cn), g.列(money_cn),
       g.列(percent_cn), g.列(standard_cost_cn), g.列(standard_cost_en)
FROM 源表(admission) h
LEFT JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = h.列(serial)
WHERE h.列(time_out) >= '{out_from}' AND h.列(time_out) < '{out_to}'
"""


def prelim_sql(out_from: str, out_to: str) -> str:
    return PRELIM_SQL.format(sortcodes=SORTCODES, out_from=out_from, out_to=out_to)


# window_from / OUT_FROM_DEFAULT / OUT_FALLBACK_TO 已抽到 window_util（与 s11 共用）：
# 两处各自写死窗口会让新增月份在一处生效、另一处被挡在窗外。


def load_official() -> pd.DataFrame:
    if not SETTLE_CSV.exists():
        raise SystemExit(f"未找到 {SETTLE_CSV}，请先运行 s01_import_settlement.py")
    return pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")


def load_prelim(out_from: str, out_to: str) -> pd.DataFrame:
    df = query_df(prelim_sql(out_from, out_to))
    df.columns = [c.lower() for c in df.columns]
    return df


# ------------------------------------------------------------------
# Stage 1: join key verification
# ------------------------------------------------------------------
def probe_keys(off: pd.DataFrame, pre: pd.DataFrame, log: list[str],
               out_from: str, out_to: str) -> dict[str, float]:
    off_no = set(off["medical_no"].dropna().astype(str).str.strip())
    off_biz = set(off["biz_no"].dropna().astype(str).str.strip())
    off_sid = set(off["settle_id"].dropna().astype(str).str.strip())

    candidates = {
        "官方[病案号] ↔ HIS[列(hospital_no)]": (off_no, pre[src_col("hospital_no")].astype(str).str.strip()),
        "官方[业务流水号] ↔ HIS[列(hospital_no)]": (off_biz, pre[src_col("hospital_no")].astype(str).str.strip()),
        "官方[病案号] ↔ HIS[列(ic_id)]": (off_no, pre[src_col("ic_id")].astype(str).str.strip()),
        "官方[结算ID] ↔ HIS[列(ic_id)]": (off_sid, pre[src_col("ic_id")].astype(str).str.strip()),
        "官方[结算ID] ↔ HIS[列(serial)]": (off_sid, pre[src_col("serial")].astype(str)),
    }

    log += ["", "## 一、主键核验", "", "| 候选关联键 | 官方命中 | 命中率 |", "|---|---:|---:|"]
    rates: dict[str, float] = {}
    for label, (off_set, his_series) in candidates.items():
        his_set = set(his_series.dropna())
        hit = len(off_set & his_set)
        rate = hit / len(off_set) if off_set else 0.0
        rates[label] = rate
        log.append(f"| {label} | {hit}/{len(off_set)} | {rate:.1%} |")

    log += [
        "",
        f"- HIS 窗口内病例（出院 {out_from} ~ {out_to}）：**{pre[src_col("serial")].nunique():,}** 例",
        f"- 官方回测集：**{len(off):,}** 例",
    ]
    return rates


# ------------------------------------------------------------------
# Stage 2: extract and join
# ------------------------------------------------------------------
def build_joined(off: pd.DataFrame, pre: pd.DataFrame, key: str) -> pd.DataFrame:
    """key = the official column matched against HIS 列(hospital_no)."""
    p = pre.copy()
    p["_hk"] = p[src_col("hospital_no")].astype(str).str.strip()
    # keep the most recent pre-grouping record per admission
    p = p.sort_values(src_col("time_rebuild")).drop_duplicates("_hk", keep="last")

    o = off.copy()
    o["_hk"] = o[key].astype(str).str.strip()
    j = o.merge(p, on="_hk", how="left", suffixes=("", "_his"))
    return j


def analyze(j: pd.DataFrame, log: list[str]) -> None:
    total = len(j)
    matched = j[src_col("group_code")].notna().sum()

    j["grp_off"] = j["drg_code"].astype(str).str.strip()
    j["grp_his"] = j[src_col("group_code")].astype(str).str.strip()
    j["grp_agree"] = j["grp_off"] == j["grp_his"]
    j["adrg_off"] = j["grp_off"].str[:3]
    j["adrg_his"] = j["grp_his"].str[:3]
    j["adrg_agree"] = j["adrg_off"] == j["adrg_his"]

    ok = j[j[src_col("group_code")].notna()]
    log += [
        "",
        "## 二、对表总体结果",
        "",
        f"- 官方回测集：**{total:,}** 例",
        f"- 关联上院内预分组：**{matched:,}** 例（{matched / total:.1%}）",
        f"- **四位码完全一致**：**{int(j['grp_agree'].sum()):,}** 例（{j['grp_agree'].mean():.1%}）",
        f"- **前三位 ADRG 一致**：**{int(j['adrg_agree'].sum()):,}** 例（{j['adrg_agree'].mean():.1%}）"
        " ← 决定分歧性质的指标",
        "",
        "> 判读：ADRG 一致而四位码不同，说明主要诊断/主要操作已正确归入同一 ADRG，"
        "差异集中在第 4 位（CC/MCC 级别或中医组标识）；ADRG 不一致才是真正的分组逻辑分歧。",
    ]

    if len(ok):
        log.append(f"- 仅统计已关联病例：四位码一致 **{ok['grp_agree'].mean():.1%}**，"
                   f"ADRG 一致 **{ok['adrg_agree'].mean():.1%}**（{len(ok):,} 例）")

    if total:
        only_digit = int((j["adrg_agree"] & ~j["grp_agree"]).sum())
        log.append(f"- 其中「ADRG 相同、仅第 4 位不同」：**{only_digit:,}** 例"
                   f"（占全量 {only_digit / total:.1%}）")

    # mismatch detail
    dis = ok[~ok["grp_agree"]].copy()
    log += ["", f"### 2.1 组码分歧明细（{len(dis):,} 例）", ""]
    if len(dis):
        pair = dis.groupby(["grp_off", "grp_his"]).size().sort_values(ascending=False).head(20)
        # attach both side group names to tell 4th-digit-only deltas from real ADRG mistakes
        name_map = {}
        for _, r in dis.iterrows():
            name_map.setdefault(("off", str(r["grp_off"])), r.get("drg_name"))
            name_map.setdefault(("his", str(r["grp_his"])), r.get(src_col("group_name")))
        log += ["| 官方组码 | 官方组名 | 院内预分组码 | 院内组名 | 例数 | 同 ADRG |",
                "|---|---|---|---|---:|---|"]
        for (a, b), n in pair.items():
            same_adrg = "是" if str(a)[:3] == str(b)[:3] else "**否**"
            log.append(
                f"| {a} | {str(name_map.get(('off', a)))[:22]} | {b} | "
                f"{str(name_map.get(('his', b)))[:22]} | {n} | {same_adrg} |"
            )

        log += ["", "### 2.2 分歧四分类（按主导特征自动初判）", ""]
        cat = _classify(dis)
        log += ["| 分歧类型 | 例数 | 占比 | 典型例子 |", "|---|---:|---|---|"]
        for name, sub in cat.items():
            ex = "、".join(f"{r.grp_off}→{r.grp_his}" for r in sub.head(3).itertuples())
            log.append(f"| {name} | {len(sub)} | {len(sub) / max(len(dis), 1):.1%} | {ex} |")

    # zero payment standard check (the TCM advantage group defect)
    log += ["", "### 2.3 院内预分组零支付标准体检", ""]
    zero = ok[ok[src_col("standard_cost")].isna() | (pd.to_numeric(ok[src_col("standard_cost")], errors="coerce") == 0)]
    log.append(f"- 院内支付标准为空或 0：**{len(zero):,}** 例（占已关联 {len(zero) / max(len(ok), 1):.1%}）")
    if len(zero):
        log.append(f"- 这些病例的院内组码分布：{zero['grp_his'].value_counts().head(8).to_dict()}")
        log.append(f"- 官方对这些病例给出的组码：{zero['grp_off'].value_counts().head(8).to_dict()}")
        agree_zero = (zero["grp_off"] == zero["grp_his"]).sum()
        log.append(f"- 其中组码与官方仍然一致的：{agree_zero} 例（说明分歧不止参数，还有规则）")

    # parameter health check
    log += ["", "### 2.4 参数体检（院内 vs 官方）", ""]
    off_rate = pd.to_numeric(ok["fee_rate"], errors="coerce").dropna()
    his_rate = pd.to_numeric(ok[src_col("basic_rate")], errors="coerce").dropna()
    log += [
        "| 参数 | 官方实际 | 院内预分组 | 偏离 |",
        "|---|---:|---:|---:|",
        f"| 基础费率 | {off_rate.median() if len(off_rate) else float('nan'):,.2f} | "
        f"{his_rate.median() if len(his_rate) else float('nan'):,.2f} | "
        f"{(his_rate.median() / off_rate.median() - 1) if len(off_rate) and len(his_rate) and off_rate.median() else float('nan'):+.1%} |",
    ]

    # primary diagnosis alignment (only months whose return carries diagnoses)
    with_diag = ok[ok["main_diag_code"].notna()]
    log += ["", "### 2.5 主要诊断口径比对（仅官方带诊断的月份）", ""]
    if len(with_diag):
        same = (with_diag["main_diag_code"].astype(str).str.strip()
                == with_diag[src_col("icd_yb")].astype(str).str.strip())
        log.append(f"- 可比对病例：**{len(with_diag):,}** 例")
        log.append(f"- 官方主要诊断码 == 院内预分组医保诊断码：**{same.sum():,}** 例（{same.mean():.1%}）")
        bad = with_diag[~same].head(8)
        if len(bad):
            log += ["", "| 官方主要诊断 | 院内预分组医保诊断 | 官方组码 | 院内组码 |", "|---|---|---|---|"]
            for r in bad.itertuples():
                log.append(f"| {r.main_diag_code} {str(r.main_diag_name)[:16]} | "
                           f"{getattr(r, src_col("icd_yb"))} {str(getattr(r, src_col("note_yb")))[:16]} | {r.grp_off} | {r.grp_his} |")
    else:
        log.append("- 无可比对病例（4/5 月返回表诊断列为空）")

    # 2.6 pre-grouping timing: was the vendor grouping produced after discharge?
    log += ["", "### 2.6 院内预分组时效性（判断低一致率是否源于「入院即分组」）", ""]
    t = ok.dropna(subset=[src_col("time_rebuild"), src_col("time_out")]).copy()
    if len(t):
        t["rebuild"] = pd.to_datetime(t[src_col("time_rebuild")], errors="coerce")
        t["out"] = pd.to_datetime(t[src_col("time_out")], errors="coerce")
        t = t.dropna(subset=["rebuild", "out"])
        t["lag_days"] = (t["out"] - t["rebuild"]).dt.days
        d = t["lag_days"].describe()
        log += [
            f"- 可比对病例：{len(t):,} 例",
            f"- 预分组时间 − 出院时间（天）：中位数 **{d['50%']:.1f}**、均值 {d['mean']:.1f}、"
            f"最小 {d['min']:.0f}、最大 {d['max']:.0f}",
            f"- 分组发生在出院前（lag>0）：{int((t['lag_days'] > 0).sum()):,} 例"
            f"（{ (t['lag_days'] > 0).mean():.1%}）",
        ]
        by_lag = t.assign(lag_bucket=pd.cut(
            t["lag_days"], [-9999, 0, 1, 3, 7, 9999],
            labels=["出院后/当日", "1天", "2-3天", "4-7天", ">7天"]))
        g = by_lag.groupby("lag_bucket", observed=True)["grp_agree"].agg(["size", "mean"])
        log += ["", "| 预分组距出院 | 例数 | 四位码一致率 |", "|---|---:|---:|"]
        for seg, r in g.iterrows():
            log.append(f"| {seg} | {int(r['size'])} | {r['mean']:.1%} |")

    # 2.7 TCM advantage group recognition vs primary operation
    log += ["", "### 2.7 中医优势病组识别：官方 IU2F vs 主要手术操作（3 月 185 例带操作编码）", ""]
    op = ok[ok["main_op_code"].notna()].copy()
    if len(op):
        op["op_code"] = op["main_op_code"].astype(str).str.strip()
        op["his_op"] = op[src_col("operation_code_yb")].astype(str).str.strip()
        op["is_tcm_off"] = op["grp_off"].str.endswith(("F", "R", "Z"))
        op["is_tcm_his"] = op["grp_his"].str.endswith(("F", "R", "Z"))

        log += ["**官方组码 × 官方主要手术编码（Top 10 操作）**", ""]
        log += ["| 官方主要手术编码 | 例数 | 官方分组分布 | 官方中医组占比 | 院内中医组占比 |",
                "|---|---:|---|---:|---:|"]
        for code, sub in sorted(op.groupby("op_code"), key=lambda x: -len(x[1]))[:10]:
            dist = sub["grp_off"].value_counts().head(3).to_dict()
            log.append(
                f"| {code} | {len(sub)} | {dist} | {sub['is_tcm_off'].mean():.1%} | "
                f"{sub['is_tcm_his'].mean():.1%} |"
            )

        log += [
            "",
            f"- 官方判为中医优势病组（F/R/Z 结尾）：**{int(op['is_tcm_off'].sum())}** 例"
            f"（{op['is_tcm_off'].mean():.1%}）",
            f"- 院内判为中医优势病组：**{int(op['is_tcm_his'].sum())}** 例"
            f"（{op['is_tcm_his'].mean():.1%}）",
            f"- 主要手术编码不一致（官方 vs 院内）：{int((op['op_code'] != op['his_op']).sum())} 例",
        ]
        bad = op[op["is_tcm_off"] & ~op["is_tcm_his"]]
        log += ["", f"**官方判中医组、院内未判的病例（{len(bad)} 例）主要手术分布：**", ""]
        for code, n in bad["op_code"].value_counts().head(12).items():
            log.append(f"- `{code}`：{n} 例")
    else:
        log.append("- 无可比对病例")


def _classify(dis: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """First-pass classification by explainable features, for manual review."""
    off = dis["grp_off"].astype(str)
    his = dis["grp_his"].astype(str)
    out: dict[str, pd.DataFrame] = {}

    # rule gap: no payment standard on the HIS side
    std = pd.to_numeric(dis[src_col("standard_cost")], errors="coerce").fillna(0)
    out["规则缺失（院内中医优势病组未识别，零支付标准）"] = dis[std == 0]

    # 4th-digit TCM flag: official Z vs HIS non-Z
    m = (off.str.endswith("Z") & ~his.str.endswith("Z")) & (std != 0)
    out["规则缺失（中医组未识别：官方 Z 组 ↔ 院内非 Z）"] = dis[m]

    # QY (ungrouped)
    m = off.str.startswith("QY") | his.str.startswith("QY")
    out["数据质量/规则（QY 未入组）"] = dis[m]

    used = pd.concat(out.values()).index if out else pd.Index([])
    rest = dis.drop(index=used, errors="ignore")

    # real logic gap: ADRG (first 3 chars) differs
    adrg_bad = rest[rest["adrg_off"] != rest["adrg_his"]]
    out["其他 ADRG 分歧（主诊断/主操作归类不同）"] = adrg_bad

    rest2 = rest.drop(index=adrg_bad.index, errors="ignore")
    out["第 4 位标识差异（ADRG 相同，CC/MCC 或中医标识不同）"] = rest2
    return {k: v for k, v in out.items() if len(v)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="仅做主键核验")
    ap.add_argument("--key", default="medical_no", help="用于匹配 HIS 住院号的官方列")
    args = ap.parse_args()

    off = load_official()
    OUT_FROM, OUT_TO = window_from(off)          # 随导入月份自适应，不再写死
    log: list[str] = [
        "# S02 差异分析打样报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 回测窗口：出院日期 {OUT_FROM} ~ {OUT_TO}（按结算返回自适应）",
        f"- 数据源：官方结算返回（s01 统一表） × 源表(admission) + 源表(vendor_pre_grouping)（既有预分组）",
    ]

    print(f"[1/3] 官方返回 {len(off):,} 例")
    pre = load_prelim(OUT_FROM, OUT_TO)
    print(f"[2/3] 院内病例 {len(pre):,} 行，{pre[src_col("serial")].nunique():,} 例")

    probe_keys(off, pre, log, OUT_FROM, OUT_TO)

    if args.probe:
        md = "\n".join(log)
        print(md)
        (OUT_SUB).mkdir(parents=True, exist_ok=True)
        (OUT_SUB / "s02_probe.md").write_text(md, encoding="utf-8")
        return 0

    j = build_joined(off, pre, args.key)
    print(f"[3/3] 对表完成，关联 {j[src_col("group_code")].notna().sum():,} 例")
    analyze(j, log)

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    # source_file / settle_ym 一并向 s08 透出：s08 靠它们推断结算月份（不再按 sheet 名写死）
    keep = ["medical_no", "settle_id", "source_sheet", "source_file", "settle_ym",
            "drg_code", "drg_name", "drg_standard",
            "weight", "fee_rate", "total_cost", "profit_loss", "refund_flag", "actual_days",
            "main_diag_code", "main_op_code",
            src_col("serial"), src_col("hospital_no"), src_col("office_now"), src_col("doctor_residency"),
            src_col("time_enter"), src_col("time_out"), src_col("inhospital_day"), src_col("money_all"),
            src_col("group_code"), src_col("group_name"), src_col("rate"), src_col("basic_rate"), src_col("grade_coefficient"),
            src_col("standard_cost"), src_col("case_type"), src_col("icd_yb"), src_col("note_yb"),
            src_col("icd_gm"), src_col("note_gm"),
            src_col("operation_code_yb"), src_col("operation_name_yb"),
            src_col("operation_code_gm"), src_col("operation_name_gm"), src_col("magnification"), src_col("time_rebuild"),
            src_col("mark_cn"), src_col("coefficient_cn"), src_col("money_cn"), src_col("percent_cn"), src_col("standard_cost_cn"),
            "grp_off", "grp_his", "grp_agree"]
    cols = [c for c in keep if c in j.columns]
    j[cols].to_csv(OUT_SUB / "prelim_joined.csv", index=False, encoding="utf-8-sig")

    md = "\n".join(log)
    (OUT_SUB / "s02_compare_report.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[完成] 报告 → {OUT_SUB / 's02_compare_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
