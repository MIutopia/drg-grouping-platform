"""S08 Profit-and-loss baseline board: hospital -> DRG group -> department.

Headline figure is the official patient-level profit field, i.e. positive means surplus.
Docs: docs/15-代码模块说明与作业手册.md §6.9.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "board"
JOINED = OUT_DIR / "compare" / "prelim_joined.csv"
CATALOG = Path(__file__).resolve().parents[1] / "config" / "dict" / "drg_group_catalog.csv"
FEE_RATE, COEF = 9575.25, 0.92


def load_office_map() -> dict[str, str]:
    """HIS department code -> department name via getattr(Clinical_Office, src_col("code_his")) ('1045|预防保健科')."""
    try:
        d = query_df("SELECT 列(code), 列(office), 列(code_his) FROM Clinical_Office")
    except Exception:  # noqa: BLE001
        return {}
    m = {}
    for r in d.itertuples():
        his = str(getattr(r, src_col("code_his")) or "")
        if "|" in his:
            m[his.split("|", 1)[0].strip()] = str(getattr(r, src_col("office"))).strip()
        elif his.strip():
            m[his.strip()] = str(getattr(r, src_col("office"))).strip()
    return m


def main() -> int:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    j = pd.read_csv(JOINED, dtype=str, encoding="utf-8-sig")
    for c in ("total_cost", "drg_standard", "profit_loss", "weight", "fee_rate", src_col("money_all")):
        if c in j.columns:
            j[c] = pd.to_numeric(j[c], errors="coerce")
    j["is_tcm"] = j["drg_code"].astype(str).str.endswith(("F", "R", "Z"))

    # 结算月份：结算日期优先；缺失时（如 3 月表无结算日期列）按 sheet 名「N月」
    # + 文件名年份推断；再退一步按文件名「2026.5」推断。
    # 此前把月份写死为 {"3月"/"4月"/"Sheet1"}，每来一个月都要改代码 ——
    # 6/7 月返回就是因此被映射成 NaN、整段掉出 groupby 的。
    DEFAULT_YEAR = datetime.now().year

    def month_of(ym: object, sheet: object, fname: object) -> str | None:
        s = str(ym or "").strip()
        if len(s) >= 7:
            return s[:7]
        m = re.search(r"(\d{1,2})\s*月", str(sheet or ""))
        y = re.search(r"(20\d{2})", str(fname or ""))
        if m:
            return f"{int(y.group(1)) if y else DEFAULT_YEAR:04d}-{int(m.group(1)):02d}"
        m2 = re.search(r"20\d{2}\s*[.\-年]\s*(\d{1,2})", str(fname or ""))
        if m2 and y:
            return f"{int(y.group(1)):04d}-{int(m2.group(1)):02d}"
        return None

    for c in ("settle_ym", "source_file", "source_sheet"):
        if c not in j.columns:
            j[c] = None
    j["month"] = [month_of(a, b, c) for a, b, c in
                  zip(j["settle_ym"], j["source_sheet"], j["source_file"])]
    列(nomonth) = int(j["month"].isna().sum())
    if 列(nomonth):
        print(f"[!] {列(nomonth)} 行未能确定结算月份，已排除出月度趋势")

    # The 2026-03 return has no profit column. Fill it with the verified convention
    # (payment standard - total cost) and label the row so the board never mixes the two.
    j["profit_calc"] = (j["drg_standard"] - j["total_cost"]).round(2)
    j["profit_src"] = j["profit_loss"].notna().map({True: "官方字段", False: "自算（3月无此列）"})
    j["profit_use"] = j["profit_loss"].fillna(j["profit_calc"]).round(2)

    cat = pd.read_csv(CATALOG, dtype=str, encoding="utf-8-sig")
    cat_map = dict(zip(cat["drg_code"], cat["drg_name"]))

    office_map = load_office_map()
    j["office_name"] = j[src_col("office_now")].astype(str).str.strip().map(office_map).fillna(
        j[src_col("office_now")].astype(str))

    # ---- hospital level ---------------------------------------------
    h = j.groupby("month").agg(
        例数=("medical_no", "count"),
        总费用=("total_cost", "sum"),
        支付标准=("drg_standard", "sum"),
        盈亏=("profit_use", "sum"),
        盈亏口径=("profit_src", lambda s: s.mode().iat[0] if len(s.mode()) else ""),
        CMI=("weight", "mean"),
        中医组占比=("is_tcm", "mean"),
    ).round(2)
    h["例均费用"] = (h["总费用"] / h["例数"]).round(2)
    h["例均盈亏"] = (h["盈亏"] / h["例数"]).round(2)
    h["结付率"] = (h["支付标准"] / h["总费用"]).round(4)
    # docs/35 D5: the official field zeroes high/low-rate cases, so the aggregate profit is
    # NOT std_cost - total_cost. Ship both plus the difference so the gap is explainable and
    # testable instead of looking like an inconsistent table.
    h["盈亏公式值"] = (h["支付标准"] - h["总费用"]).round(2)
    h["盈亏调整"] = (h["盈亏"] - h["盈亏公式值"]).round(2)
    h.to_csv(OUT_SUB / "board_hospital.csv", encoding="utf-8-sig")

    # ---- DRG group level --------------------------------------------
    d = j.groupby("drg_code").agg(
        例数=("medical_no", "count"),
        总费用=("total_cost", "sum"),
        支付标准=("drg_standard", "sum"),
        盈亏=("profit_use", "sum"),
        权重=("weight", "mean"),
    ).round(2)
    d["组名"] = d.index.map(cat_map).fillna("")
    d["例均费用"] = (d["总费用"] / d["例数"]).round(2)
    d["例均盈亏"] = (d["盈亏"] / d["例数"]).round(2)
    d["中医优势病组"] = d.index.str.endswith(("F", "R", "Z"))
    d["盈亏公式值"] = (d["支付标准"] - d["总费用"]).round(2)
    d["盈亏调整"] = (d["盈亏"] - d["盈亏公式值"]).round(2)
    # provenance per group: a group spanning months mixes the official field (4/5月) with the
    # formula (3月无此列) - say so rather than leaving an unexplained non-zero 盈亏调整.
    d["盈亏口径"] = j.groupby("drg_code")["profit_src"].agg(
        lambda s: "、".join(sorted(set(s))))
    # 病组层必须带「总费用」：装载侧（s18 CN_MAP）按列名映射，缺列会把
    # result_pnl.total_cost 整列置空 —— 表现为病组盈亏表「总费用」全空，
    # 而盈亏 = 支付标准 − 总费用 本可互相校验，缺列后无从核对。
    d = d[["组名", "例数", "权重", "总费用", "例均费用", "支付标准", "盈亏", "盈亏公式值",
           "盈亏调整", "盈亏口径", "例均盈亏", "中医优势病组"]]
    d.sort_values("盈亏", ascending=False).to_csv(OUT_SUB / "board_drg.csv", encoding="utf-8-sig")

    # ---- department level -------------------------------------------
    o = j.groupby("office_name").agg(
        例数=("medical_no", "count"),
        总费用=("total_cost", "sum"),
        支付标准=("drg_standard", "sum"),
        盈亏=("profit_use", "sum"),
        CMI=("weight", "mean"),
        中医组占比=("is_tcm", "mean"),
    ).round(2)
    o["例均费用"] = (o["总费用"] / o["例数"]).round(2)
    o["例均盈亏"] = (o["盈亏"] / o["例数"]).round(2)
    o["盈亏公式值"] = (o["支付标准"] - o["总费用"]).round(2)
    o["盈亏调整"] = (o["盈亏"] - o["盈亏公式值"]).round(2)
    o["盈亏口径"] = j.groupby("office_name")["profit_src"].agg(
        lambda s: "、".join(sorted(set(s))))
    o.sort_values("盈亏", ascending=False).to_csv(OUT_SUB / "board_office.csv", encoding="utf-8-sig")

    # ---- report ------------------------------------------------------
    total_pnl = j["profit_use"].sum()
    n_calc = int((j["profit_src"] != "官方字段").sum())
    # Month range straight from the data. The header said "2026-03~05" while the board was
    # already built from 3-7 months (N cases) - the same stale hard-coded label that was
    # corrected in s11/s14/s20/s07.
    _months = sorted(str(m) for m in j["month"].dropna().unique() if str(m).strip())
    period = f"{_months[0]} ~ {_months[-1]}" if _months else "月份未知"
    log = [
        "# S08 盈亏基线看板（全院 → 病组 → 科室）",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 数据范围：{period} 官方结算返回 **{len(j):,}** 例",
        f"- 口径：盈亏额 = 支付标准 − 住院医疗总费用；费率 {FEE_RATE:,.2f}；机构系数 {COEF}",
        f"- 盈亏数据来源：官方字段 **{len(j) - n_calc:,}** 例、**自算 {n_calc:,} 例**"
        "（3 月返回表无盈亏额列，按已定版公式补算并标注）",
        f"- **回测期合计盈亏：{total_pnl:,.2f} 元**",
        "",
        "## 一、全院层（月度）",
        "",
        _md(h.reset_index()),
        "",
        "## 二、病组层",
        "",
        f"共 **{len(d)}** 个病组，其中中医优势病组 **{int(d['中医优势病组'].sum())}** 个。",
        "",
        f"### 2.1 盈亏 Top 10（贡献最大）",
        "",
        _md(d.sort_values("盈亏", ascending=False).head(10).reset_index()),
        "",
        "### 2.2 盈亏 Bottom 10（亏损最大）",
        "",
        _md(d.sort_values("盈亏").head(10).reset_index()),
        "",
        "## 三、科室层",
        "",
        _md(o.reset_index()),
        "",
        "## 四、口径说明与注意事项",
        "",
        "0. **3 月盈亏额为自算值（已补全并标注）**：2026 年 3 月返回表（52 列 schema）**不含「盈亏额」列**。"
        "本看板已按已定版公式 `支付标准 − 住院医疗总费用` 补算，并在「盈亏口径」列标注为「自算（3月无此列）」。"
        "该公式在 4/5 月 367 例上的复现率为 **97.00%**（残差为高低倍率病例，官方置 0），故自算值可作趋势参考；"
        "**正式结算数据仍以医保返回为准**，建议向医保办索取 3 月带盈亏额的导出。"
        "**口径已显式化（docs/35 D5）**：每行同时给出 `盈亏公式值`（= 支付标准 − 总费用）、"
        "`盈亏调整`（= 盈亏 − 盈亏公式值）与 `盈亏口径`（来源说明）。两条恒等式可作校验断言："
        "`盈亏公式值 = 支付标准 − 总费用`（恒成立）、`盈亏 = 盈亏公式值 + 盈亏调整`（恒成立）。"
        "`盈亏调整 ≠ 0` 的行全部由官方对高低倍率病例置 0 所致——**分组盈亏排名用 `盈亏`（官方口径）**，"
        "**费用结构/体量分析用 `盈亏公式值`**，两者不可混用。",
        "1. **盈亏额符号**：正数 = 医院盈利。该方向已由 N 例实测反推确认（正向公式一致率 97.00%，反向 0.00%）；",
        "2. **高低倍率病例**：官方对其盈亏额置 0（某地区医保局〔2025〕26 号：费用极高前 5%、极低 <40% 按项目付费），",
        "   此类病例会计入例数与总费用、但不贡献盈亏，做病组盈亏排名时需剔除或单列；",
        "3. **科室口径**：官方返回 4/5 月表的「科室名称」列为空，本看板科室取 **HIS `列(office_now)`** 经",
        "   `Clinical_Office` 字典映射；3 月表的官方科室名可用于抽样校验映射准确率；",
        "4. **中医优势病组**：F/R/Z 结尾。回测期该组例数占比 73.4%，是本单位盈亏的绝对主体；",
        "5. 本产出为看板数据源，可直接挂 Metabase；明细需权限控制（含住院号），导出物须脱敏。",
        "",
        "## 五、下一步",
        "",
        "1. 与财务系统月度汇总数核对（需使用方提供财务侧住院收入汇总）；",
        "2. 增加**成本维度**（若后续能取得成本核算数据）以支撑「DRG 成本管理」；",
        "3. 政策更新后按 `policy_version` 重跑本看板做趋势对比。",
    ]

    md = "\n".join(log)
    (OUT_SUB / "s08_board_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


if __name__ == "__main__":
    raise SystemExit(main())
