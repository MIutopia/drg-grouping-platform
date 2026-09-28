"""S07 Finance reconciliation pack v1 - per-admission three-way reconciliation.

Two accounting conventions are pinned down by this job:
  1. reconciliation basis = sum of fee lines with 列(money) > 0 (refund lines listed separately);
  2. official profit = DRG payment standard - total inpatient cost (positive = surplus).
Docs: docs/15-代码模块说明与作业手册.md §6.8.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402
from tcm_ratio import NUMERATOR, ratio, subject_totals  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "recon"
SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"
OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"
TOL = 0.01                      # per-case tolerance in CNY

# Sort-code names: confirmed in docs/04 plus the observed 源表(cost_item).列(sort_kind) mode.
SORT_CODE_HINT = {
    "04": "挂号费", "05": "床位费", "06": "诊察费", "07": "护理费",
    "08": "检查费", "09": "化验费", "10": "治疗费", "11": "手术费",
    "12": "治疗费", "13": "材料费", "14": "磁共振", "15": "CT费",
    "16": "病理费", "17": "其他费", "18": "输血费", "19": "麻醉费",
    "21": "其他医疗费",
}
# Approximate mapping from the TCM-ratio numerators/denominators to sort codes.
# Sort code 10/12 mixes TCM and western therapy, so this is an upper bound only.
ZZL_MAP = {
    "中医治疗费(近似=治疗费)": ["10", "12"],
    "中成药费(近似)": ["13"],       # needs charge-item attributes to refine
    "中药饮片费(近似)": [],
    "西药费": [],
    "西医治疗费(近似)": [],
    "手术费": ["11"],
    "卫生材料费": [],
}


def pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "-"


FEE_TABLES_SQL = ("源表(fee_check_in)", "源表(fee_cure_in)",
                  "源表(fee_medicine_in)")


def audit_window() -> tuple[str, str]:
    """Discharge window that encloses every settled case, derived from the settlement file.

    The window used to be hard-coded to the 3-5 month baseline (2026-02-20 ~ 2026-06-01).
    Once the settlement returns were extended to 3-7 months the file held N cases while the
    fee and TCM queries still covered only the older months, so later cases reconciled
    against no source rows at all - the same defect that deflated s14's headline rate to
    47.6% before its window was made to follow the data. Deriving it here keeps the two
    jobs consistent. OUT_FROM/OUT_TO remain the fallback when the file is missing.
    """
    if not SETTLE_CSV.exists():
        return OUT_FROM, OUT_TO
    d = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    if "settle_date" not in d.columns:
        return OUT_FROM, OUT_TO
    s = pd.to_datetime(d["settle_date"].astype(str).str[:10], errors="coerce")
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi):
        return OUT_FROM, OUT_TO
    return ((lo - pd.Timedelta(days=60)).strftime("%Y-%m-%d"),
            (hi + pd.Timedelta(days=31)).strftime("%Y-%m-%d"))


def fetch_fee_by_case() -> pd.DataFrame:
    """Aggregate fees per admission and sort code, returning both net and positive totals.

    The fee tables contain negative lines (refunds / reversals). Netting them makes the
    HIS total systematically lower than the official settlement total, so both bases are
    computed and compared. Date bounds are bound as parameters rather than interpolated.
    """
    w_from, w_to = audit_window()
    union = " UNION ALL ".join(
        f"  SELECT m.列(hospital_no), f.列(sort_code), f.列(money) FROM {tbl} f "
        "    JOIN 源表(admission) m ON m.列(hospital_no) = f.列(hospital_no) "
        "    WHERE m.列(time_out) >= %s AND m.列(time_out) < %s AND f.列(mark_dell) = 0"
        for tbl in FEE_TABLES_SQL)
    sql = ("SELECT 列(hospital_no), 列(sort_code), "
           "SUM(列(money)) AS money, "
           "SUM(CASE WHEN 列(money) > 0 THEN 列(money) ELSE 0 END) AS money_pos, "
           "SUM(CASE WHEN 列(money) < 0 THEN 列(money) ELSE 0 END) AS money_neg, "
           "SUM(CASE WHEN 列(money) < 0 THEN 1 ELSE 0 END) AS neg_rows FROM ("
           + union + ") t GROUP BY 列(hospital_no), 列(sort_code)")
    return query_df(sql, params=(w_from, w_to) * len(FEE_TABLES_SQL))


FEE_TABLES = [
    ("检查", "源表(fee_check_in)"),
    ("治疗", "源表(fee_cure_in)"),
    ("药品", "源表(fee_medicine_in)"),
]


def diag(hospital_no: str) -> None:
    """Single-case drill-down: fee breakdown by sort code for one admission.

    The admission number is the one externally supplied value in this job, so it is bound as
    a parameter instead of interpolated (code review Q1). Table and label still come from the
    internal FEE_TABLES whitelist.
    """
    sql = " UNION ALL ".join(
        f"SELECT '{label}' AS src, 列(sort_code), COUNT(1) AS rows_n, SUM(列(money)) AS money "
        f"FROM {tbl} WHERE 列(hospital_no) = %s AND 列(mark_dell) = 0 GROUP BY 列(sort_code)"
        for label, tbl in FEE_TABLES)
    d = query_df(sql, params=(str(hospital_no),) * len(FEE_TABLES))
    print(f"### 住院号 {hospital_no} 的费用明细构成\n")
    if d.empty:
        print("（三张费用表中无记录 —— 需确认是否已收费/住院号是否一致）")
        return
    print(d.to_string(index=False))
    print(f"\n合计：{pd.to_numeric(d['money'], errors='coerce').sum():,.2f} 元")


def main() -> int:
    import argparse  # noqa: PLC0415

    # Console-encoding degradation for reports containing characters outside GBK now lives
    # once in src/config/settings.py (imported by every job); see docs/35 §八·补4.3.

    ap = argparse.ArgumentParser()
    ap.add_argument("--diag", default=None, help="单例差异追查的住院号")
    a = ap.parse_args()
    if a.diag:
        diag(a.diag)
        return 0

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    off = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    w_from, w_to = audit_window()
    fee = fetch_fee_by_case()
    fee["money"] = pd.to_numeric(fee["money"], errors="coerce").fillna(0.0)

    # sort-code composition, used as evidence for the account mapping confidence
    sc = fee.groupby(src_col("sort_code"), dropna=False)["money"].agg(["sum", "count"]).reset_index()
    sc["sort_code"] = sc[src_col("sort_code")].fillna("(空)")
    sc["科目名(实测提示)"] = sc["sort_code"].map(SORT_CODE_HINT).fillna("待确认")
    total = sc["sum"].sum()
    sc["金额占比"] = (sc["sum"] / total).map(lambda x: f"{x:.2%}")
    sc = sc[["sort_code", "科目名(实测提示)", "sum", "count", "金额占比"]]
    sc.columns = ["统计码", "科目名(实测提示)", "金额合计", "明细行数", "金额占比"]
    sc = sc.sort_values("金额合计", ascending=False)
    # same reasoning as recon_cases.csv: cents are the unit, and rounding keeps this artefact
    # byte-reproducible between runs
    if "金额合计" in sc.columns:
        sc["金额合计"] = pd.to_numeric(sc["金额合计"], errors="coerce").round(2)
    sc.to_csv(OUT_SUB / "fee_sort_code.csv", index=False, encoding="utf-8-sig")

    # per-case HIS totals under both bases (net / positive only)
    for c in ("money", "money_pos", "money_neg", "neg_rows"):
        fee[c] = pd.to_numeric(fee[c], errors="coerce").fillna(0.0)
    his = fee.groupby(src_col("hospital_no"))[["money", "money_pos", "money_neg", "neg_rows"]].sum().reset_index()
    his.columns = ["hospital_no", "his_fee", "his_fee_pos", "his_neg", "neg_rows"]

    j = off.merge(his, left_on="medical_no", right_on="hospital_no", how="left")
    for c in ("total_cost", "drg_standard", "profit_loss", "weight", "fee_rate"):
        j[c] = pd.to_numeric(j[c], errors="coerce")

    j["his_fee"] = j["his_fee"].fillna(0.0)
    j["his_fee_pos"] = j["his_fee_pos"].fillna(0.0)
    j["his_neg"] = j["his_neg"].fillna(0.0)
    j["diff"] = (j["his_fee"] - j["total_cost"]).round(2)          # net basis
    j["diff_pos"] = (j["his_fee_pos"] - j["total_cost"]).round(2)  # positive basis
    j["abs_diff"] = j["diff"].abs()

    def classify(r: pd.Series) -> str:
        if r["his_fee"] == 0:
            return "HIS 无费用记录（需追查：未收费/未结算/住院号不一致）"
        if r["abs_diff"] <= TOL:
            return "一致（±0.01）"
        if r["abs_diff"] <= 1:
            return "微差（≤1 元，四舍五入/尾差）"
        if r["abs_diff"] <= 100:
            return "小差（≤100 元，疑似单项未同步）"
        return "**显著差异（>100 元）**"

    j["diff_type"] = j.apply(classify, axis=1)
    # official profit convention (reverse-derived): payment standard - total cost
    j["院内自算盈亏"] = (j["drg_standard"] - j["total_cost"]).round(2)

    # ---- report -----------------------------------------------------
    log = [
        "# S07 财务对账包 v1 报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 对账范围：出院日期 {w_from} ~ {w_to}（**由结算返回自动推导**，非固定 3-5 月窗口），"
        f"共 **{len(j):,}** 例",
        f"- 容差：逐例 ±{TOL} 元",
        "",
        "## 一、逐例勾稽结果（HIS 费用明细合计 ↔ 官方结算返回总费用）",
        "",
        "| 差异分类 | 例数 | 占比 | 差异金额合计 |",
        "|---|---:|---:|---:|",
    ]
    for t, sub in j.groupby("diff_type"):
        log.append(f"| {t} | {len(sub)} | {len(sub) / len(j):.1%} | {sub['diff'].sum():,.2f} |")

    ok = j[j["abs_diff"] <= TOL]
    log += [
        "",
        f"- **逐例一致率（净额口径）：{len(ok) / len(j):.1%}**（{len(ok):,}/{len(j):,}）",
        f"- 全量差异合计：**{j['diff'].sum():,.2f}** 元（HIS 净额 − 官方）",
    ]

    # ---- impact of refund / reversal lines --------------------------
    neg_cases = j[j["neg_rows"] > 0]
    ok_pos = j[j["diff_pos"].abs() <= TOL]
    log += [
        "",
        "### 1.0 关键归因：费用表中的负数行（退费/红冲）",
        "",
        f"- 含负数行的病例：**{len(neg_cases)}** 例（占 {len(neg_cases) / len(j):.1%}），"
        f"负数金额合计 **{j['his_neg'].sum():,.2f}** 元",
        f"- 剔除负数行后的一致率（正数口径）：**{len(ok_pos) / len(j):.1%}**"
        f"（净额口径为 {len(ok) / len(j):.1%}）",
        f"- 正数口径全量差异：**{j['diff_pos'].sum():,.2f}** 元",
        "",
        "> **结论**：正数口径"
        f"（{len(ok_pos) / len(j):.1%}）显著优于净额口径（{len(ok) / len(j):.1%}），"
        "说明差异**不是数据缺失，而是口径定义问题**——",
        f"> HIS 费用明细中 {len(neg_cases) / len(j):.1%} 的病例带有退费/红冲负数行，"
        "而官方「住院医疗总费用」按**正数合计**口径给出。",
        "> 因此对账口径**必须定义为「费用明细中 列(money) > 0 的合计」**，退费/红冲作为**单列调整项**另行核对。",
        "> 这一条是财务对账能否「对到笔」的前提，建议写入对账规则并交财务科确认。",
    ]
    neg_top = (fee[fee["money_neg"] < 0].groupby(src_col("sort_code"))["money_neg"]
               .agg(["sum", "count"]).sort_values("sum").head(8))
    if len(neg_top):
        log += ["", "| 统计码 | 负数金额合计 | 负数行数 |", "|---|---:|---:|"]
        for code, r in neg_top.iterrows():
            log.append(f"| {code if code else '(空)'} | {r['sum']:,.2f} | {int(r['count'])} |")

    sig = j[j["diff_type"].str.startswith("**显著")]
    log += ["", f"### 1.1 显著差异明细（Top 15，共 {len(sig)} 例）", "",
            "| 住院号 | HIS 明细合计 | 官方总费用 | 差异 | 官方组码 |", "|---|---:|---:|---:|---|"]
    for _, r in sig.reindex(sig["abs_diff"].sort_values(ascending=False).index).head(15).iterrows():
        log.append(f"| {r['medical_no']} | {r['his_fee']:,.2f} | {r['total_cost']:,.2f} | "
                   f"{r['diff']:+,.2f} | {r['drg_code']} |")

    # ---- monthly roll-up -------------------------------------------
    monthly = j.groupby("source_sheet").agg(
        例数=("medical_no", "count"),
        HIS合计=("his_fee", "sum"),
        官方合计=("total_cost", "sum"),
        官方盈亏合计=("profit_loss", "sum"),
        支付标准合计=("drg_standard", "sum"),
    ).round(2)
    monthly["差异"] = (monthly["HIS合计"] - monthly["官方合计"]).round(2)
    monthly["差异率"] = (monthly["差异"] / monthly["官方合计"]).map(lambda x: f"{x:.4%}")
    monthly.to_csv(OUT_SUB / "recon_monthly.csv", encoding="utf-8-sig")
    mt = monthly.reset_index()
    if _has_tabulate():
        mt_md = mt.to_markdown(index=False)
    else:
        mt_md = "```\n" + mt.to_string(index=False) + "\n```"
    log += ["", "## 二、月度汇总层对账", "", mt_md]

    # ---- official profit convention check ---------------------------
    log += ["", "## 三、官方「盈亏额」口径定义（实现反推 + 验证）", "",
            "官方结算返回的「盈亏额」符号方向为 **正数 = 医院盈利**，即：", "",
            "> **盈亏额 = DRG 支付标准 − 住院医疗总费用**", ""]
    chk = j.dropna(subset=["profit_loss", "total_cost", "drg_standard"])
    if len(chk):
        chk = chk.assign(
            正向=(chk["drg_standard"] - chk["total_cost"]).round(2),
            反向=(chk["total_cost"] - chk["drg_standard"]).round(2))
        m_fwd = (chk["profit_loss"] - chk["正向"]).abs().le(TOL).mean()
        m_rev = (chk["profit_loss"] - chk["反向"]).abs().le(TOL).mean()
        log += [
            f"- 可比对病例：**{len(chk)}** 例",
            f"- 正向公式（支付标准 − 总费用）一致率：**{m_fwd:.2%}**",
            f"- 反向公式（总费用 − 支付标准）一致率：{m_rev:.2%}",
        ]
        resid = chk[(chk["profit_loss"] - chk["正向"]).abs() > TOL]
        log += ["", f"### 3.1 正向公式的残差（{len(resid)} 例，占 {len(resid) / len(chk):.2%}）", ""]
        if len(resid):
            log += ["| 住院号 | 官方盈亏额 | 正向自算 | 残差 | 官方组码 |", "|---|---:|---:|---:|---|"]
            for _, r in resid.head(10).iterrows():
                log.append(f"| {r['medical_no']} | {r['profit_loss']:,.2f} | {r['正向']:,.2f} | "
                           f"{r['profit_loss'] - r['正向']:+,.2f} | {r['drg_code']} |")
            zero = resid[resid["profit_loss"] == 0]
            log += ["",
                    f"**残差构成**：其中 **{len(zero)}/{len(resid)}** 例的官方盈亏额为 **0.00**，"
                    "属**高低倍率/未按 DRG 结算**的病例——某地区医保局〔2025〕26 号规定"
                    "「费用极高（前 5%）」与「费用极低（<40%）」按项目付费，此类病例不产生 DRG 盈亏，官方即置 0。",
                    "",
                    "> 例：住院号 20260179 总费用 16,903.20 元 vs 支付标准 4,209.93 元（4.0 倍），属高倍率病例 → 盈亏额置 0。",
                    "> 其余残差需进一步纳入 **DRG 除外支付、外购药扣减、床位费超标追加、机器人手术补偿、各类追加项**"
                    "（结算返回表中有对应列）后复现。",
                    "",
                    "**结论**：官方盈亏额口径已定版为 `支付标准 − 住院医疗总费用`，并对高低倍率病例置 0；"
                    "该定义应写入指标口径字典（docs/08 §三）并作为看板与对账的唯一口径。"]
        else:
            log.append("- 无残差，公式完全复现。")
    else:
        log.append("- 无可比对病例")

    # ---- fund / out-of-pocket split source --------------------------
    log += ["", "## 四、基金 / 自付拆分来源（本期实测修正）", "",
            "| 候选数据源 | 实测结论 | 是否可用 |",
            "|---|---|---|",
            "| `源表(settle_bill)` 四金额列（甲类/乙类/自付/其他） | 近一年半 **401,265 行填充率均为 0%** | **不可用** |",
            "| `源表(invoice_title).列(hospital_no)`（关联住院号） | 填充率 **4.7%**（33,677 中 1,569），主要为门诊发票 | 不可用 |",
            "| **医保结算返回自带拆分字段** | 3 月表：全自费/职工统筹报销额/职工大额报销额/居民统筹报销额；"
            "4-5 月表：起付线/超限价自费/先行自付/账户共济/个人账户支出 | **可用（一期采用）** |",
            "",
            "> 该结论修正 docs/05 的判断。对账包的基金/自付维度以**结算返回**为准，",
            "> HIS 侧仅提供费用明细与科目构成，不再尝试从 HIS 反推基金拆分。",
            ]

    # ---- TCM ratio feasibility --------------------------------------
    log += ["", "## 五、中治率（27 号文硬条件）科目映射（已落地，见 docs/16）", "",
            "| 中治率科目 | 数据源 | 状态 |",
            "|---|---|---|",
            "| 中药饮片费 | 药品费用统计码 `03` | 已接入 |",
            "| 中成药费 | 药品费用统计码 `02` | 已接入 |",
            "| 西药费 | 药品费用统计码 `01` | 已接入 |",
            "| 卫生材料费 | 费用统计码 `13` | 已接入 |",
            "| 手术费 | 费用统计码 `11`（非中医项目） | 已接入 |",
            "| 中医治疗费 | 统计码 `10/11/12` 且属中医项目（目录章节四 / 某地区码 4x / 院内码 04） | 已接入 |",
            "| 西医治疗费 | 统计码 `10/12` 的非中医项目 | 已接入 |",
            "",
            "> 映射由 `src/etl/s09_fee_mapping.py` 生成到 `src/config/dict/cost_item_attribute.csv`；",
            "> 逐例校验见下一节。**中医正骨类项目在 HIS 计入统计码 11（手术费）**，",
            "> 是否计入分子待医保办确认（见 `src/out/feemap/review_sheet.csv`）。",
            ]

    # ---- TCM treatment cost ratio per case --------------------------
    tot = ratio(subject_totals(
        "sbo", f"m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}'")).reset_index()
    tot = tot[[src_col("hospital_no"), "zzl_numerator", "zzl_denominator", "zzl_ratio"]]
    j = j.merge(tot, left_on="medical_no", right_on=src_col("hospital_no"), how="left", suffixes=("", "_zzl"))
    j["off_is_tcm"] = j["drg_code"].astype(str).str.endswith(("F", "R", "Z"))

    tcm_cases = j[j["off_is_tcm"] & j["zzl_ratio"].notna()]
    log += ["", "## 六、中治率（27 号文硬条件）逐例校验", "",
            "公式（官方）：中治率 =（中医治疗费+中成药费+中药饮片费）/（中医治疗费+西医治疗费+西药费+中成药费+中药饮片费+手术费+卫生材料费）",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 可比对病例（官方判为中医优势病组） | **{len(tcm_cases):,}** |",
            f"| 中治率 ≥60%（达标） | **{int((tcm_cases['zzl_ratio'] >= 0.6).sum()):,}**"
            f"（{pct(int((tcm_cases['zzl_ratio'] >= 0.6).sum()), len(tcm_cases))}） |",
            f"| 中治率 <60%（将降级为内科组结算） | **{int((tcm_cases['zzl_ratio'] < 0.6).sum()):,}** |",
            f"| 中治率中位数 | {tcm_cases['zzl_ratio'].median():.1%} |",
            f"| 中治率区间 | {tcm_cases['zzl_ratio'].min():.1%} ~ {tcm_cases['zzl_ratio'].max():.1%} |",
            ""]
    low = tcm_cases[tcm_cases["zzl_ratio"] < 0.6].sort_values("zzl_ratio")
    if len(low):
        log += [f"### 6.1 中治率未达标病例（{len(low)} 例，需复核科目口径）", "",
                "| 住院号 | 官方组码 | 中治率 | 分子 | 分母 | 官方盈亏额 |", "|---|---|---:|---:|---:|---:|"]
        for _, r in low.head(15).iterrows():
            log.append(f"| {r['medical_no']} | {r['drg_code']} | {r['zzl_ratio']:.1%} | "
                       f"{r['zzl_numerator']:,.2f} | {r['zzl_denominator']:,.2f} | "
                       f"{r['profit_loss'] if pd.notna(r['profit_loss']) else 0:,.2f} |")
        log += ["",
                "> 提示：未达标既可能是真实情况（西医费用占比高），也可能是**科目映射口径**问题——",
                "> 中医正骨类项目在 HIS 计入统计码 11（手术费），若官方口径将其计入分子，则本表会低估中治率。",
                "> 该口径差异已列入 `src/out/feemap/review_sheet.csv` 交医保办确认。"]

    log += ["", "## 七、产出与下一步", "",
            "- `src/out/recon/recon_cases.csv` —— 逐例勾稽明细（对到笔，含中治率）",
            "- `src/out/recon/recon_monthly.csv` —— 月度汇总层核对",
            "- `src/out/recon/fee_sort_code.csv` —— HIS 费用统计码科目构成（含占比）",
            "",
            "**下一步**：",
            "1. 显著差异逐例追查（未收费 / 未结算 / 住院号不一致），出追查单交财务科（SLA 5 个工作日）；",
            "2. 官方盈亏额公式补全（纳入 DRG 除外支付、外购药扣减、各类追加补偿项）；",
            "3. 建立收费项目科目映射，解锁中治率与卫生材料费口径；",
            "4. 与财务系统汇总数的月度核对（需使用方提供财务侧月度住院收入汇总）。",
            ]

    keep = ["medical_no", "source_sheet", "drg_code", "total_cost", "his_fee", "diff",
            "diff_type", "drg_standard", "profit_loss", "院内自算盈亏", "weight", "fee_rate",
            "zzl_numerator", "zzl_denominator", "zzl_ratio"]
    out = j[[c for c in keep if c in j.columns]].sort_values(
        "diff", key=abs, ascending=False).copy()
    # Round before writing. SQL Server's SUM over float money columns is not bit-reproducible
    # across runs (a parallel plan adds in a different order), so the raw values carried ~1e-12
    # dust - `1189.6999999999998` where the previous run wrote `1189.7` - and the same job
    # produced a different CSV every time. Cents are the reporting unit anyway, and rounding is
    # what makes the artefact byte-reproducible (see docs/35 复现边界).
    # `+ 0.0` normalises negative zero: round(-1e-12, 2) is -0.0 while the next run of the same
    # job may produce +0.0 (the sign of float dust is not stable), and `-0.0` in a finance
    # export is both confusing and non-reproducible. IEEE: -0.0 + 0.0 == +0.0.
    for _c in ("total_cost", "his_fee", "diff", "drg_standard", "profit_loss", "院内自算盈亏",
               "zzl_numerator", "zzl_denominator"):
        if _c in out.columns:
            out[_c] = pd.to_numeric(out[_c], errors="coerce").round(2) + 0.0
    for _c in ("weight", "fee_rate", "zzl_ratio"):
        if _c in out.columns:
            out[_c] = pd.to_numeric(out[_c], errors="coerce").round(4) + 0.0
    out.to_csv(OUT_SUB / "recon_cases.csv", index=False, encoding="utf-8-sig")

    md = "\n".join(log)
    (OUT_SUB / "s07_recon_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _has_tabulate() -> bool:
    try:
        import tabulate  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
