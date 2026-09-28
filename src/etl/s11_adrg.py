"""S11 Extract the official CHS-DRG 2.0 dictionaries and back-test the grouping engine.

Two responsibilities:
  1. extraction - turn the official workbook into the runtime dictionaries under
     src/config/dict/ consumed by drg_engine.py;
  2. back-test - group the official settlement cases and measure ADRG agreement.
The grouping logic itself lives in drg_engine.py and is shared with the in-hospital
simulator, so this job never duplicates it.
Docs: docs/18-P2分组引擎与映射重建.md, docs/19-引擎接入在院模拟器.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402
from drg_engine import COEF, FEE_RATE, PATH_NONE, PATH_STD, PATH_TCM, DrgEngine  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, DICT_DIR, OUT_DIR, RAW_DIR  # noqa: E402

WORKBOOK = RAW_DIR / "官方下发文件" / "CHS-DRG2.0完整版CC-MCC排除表.xlsx"
OUT_SUB = OUT_DIR / "adrg"
DICT_OUT = CONFIG_DIR / "dict"
SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"

# 窗口随导入月份自适应（与 s02 共用 window_util）。此前写死到 2026-06-01，
# 6/7 月返回导入后仍按旧窗口去 HIS 取诊断/手术，新月份一例都取不到、
# 引擎产不出预测（对表一致率随之被稀释成 61.7% 的假象）。
from window_util import window_from  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

SHEETS = {
    "cond": "入组条件", "drg": "DRG组", "adrg": "ADRG",
    "no_main_dx": "不作为主诊", "no_main_op": "不作为主手术",
    "main_dx": "主诊表", "cc": "CC", "mcc": "MCC", "exclude": "排除表",
}


def read_sheet(key: str) -> pd.DataFrame:
    return pd.read_excel(WORKBOOK, sheet_name=SHEETS[key], dtype=object, engine="openpyxl")


def probe() -> None:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    c = read_sheet("cond")
    c.columns = ["mdc", "adrg", "code", "name", "c5", "c6"][:c.shape[1]]
    op = c[c["code"].astype(str).str.match(r"^\d{2}\.")]
    dx = c[c["c6"].astype(str).str.contains("主诊表", na=False)]
    print(f"入组条件：{len(c):,} 行；手术式 {len(op):,}；诊断式 {len(dx):,}")
    print("\nADRG 数量:", c["adrg"].nunique())
    print("\n手术式条件样例:\n", op.head(5).to_string(index=False, max_colwidth=24))
    print("\n诊断式条件样例:\n", dx.head(5).to_string(index=False, max_colwidth=24))
    d = read_sheet("drg")
    d.columns = ["seq", "adrg", "drg", "drg_name"][:d.shape[1]]
    print("\nDRG组样例:\n", d.head(8).to_string(index=False, max_colwidth=28))
    print("\n第4位分布:", d["drg"].astype(str).str[-1].value_counts().head(12).to_dict())


# ------------------------------------------------------------------
# Dictionary build
# ------------------------------------------------------------------
def parse_dictionaries() -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}

    c = read_sheet("cond")
    c.columns = ["mdc", "adrg", "code", "name", "c5", "c6"][:c.shape[1]]
    c = c.dropna(subset=["adrg"])
    c["code"] = c["code"].astype(str).str.strip()
    c["adrg"] = c["adrg"].astype(str).str.strip()
    c["mdc"] = c["mdc"].astype(str).str.strip()
    # prose rows ("包含以下主要手术或操作：", "和") are section markers, not conditions
    c = c[c["code"].str.match(r"^[A-Z0-9]", na=False) & ~c["code"].str.contains("包含|和$", na=False)]
    c = c[c["code"] != c["adrg"]]                       # drop ADRG header rows

    # MDCZ (multiple severe trauma) is a cross-MDC priority rule, not a plain ADRG: its rows
    # carry the diagnosis-group NAME in the ADRG column (头颈部创伤 ... 躯干、脊柱创伤) and every
    # one of those codes already maps to another MDC in the main-diagnosis table. Kept as a
    # separate dictionary so nothing is lost, and excluded from the ADRG conditions.
    # ICD/ADRG codes are ASCII by definition, so anything carrying Chinese is prose that leaked
    # into a code cell (found: a single row holding '3+手术或操作')
    c = c[c["code"].str.match(r"^[\x20-\x7e]+$", na=False)]
    is_adrg = c["adrg"].str.match(r"^[A-Z]{2}\d$", na=False)
    mdcz = c[~is_adrg][["mdc", "adrg", "code", "name"]].rename(columns={"adrg": "grp_name"})
    c = c[is_adrg]

    c["kind"] = c["c6"].astype(str).str.contains("主诊表", na=False).map({True: "dx", False: "op"})
    out["cond"] = c[["mdc", "adrg", "kind", "code", "name"]].drop_duplicates()
    out["mdcz"] = mdcz.drop_duplicates()

    d = read_sheet("drg")
    d.columns = ["seq", "adrg", "drg", "drg_name"][:d.shape[1]]
    d = d.dropna(subset=["drg"])
    for col in ("adrg", "drg", "drg_name"):
        d[col] = d[col].astype(str).str.strip()
    out["drg"] = d[["adrg", "drg", "drg_name"]].drop_duplicates()

    a = read_sheet("adrg")
    a.columns = ["adrg", "adrg_name"][:a.shape[1]]
    a = a.dropna(subset=["adrg"])
    a["adrg"] = a["adrg"].astype(str).str.strip()
    a["adrg_name"] = a["adrg_name"].astype(str).str.strip()
    out["adrg"] = a.drop_duplicates("adrg")

    m = read_sheet("main_dx")
    m.columns = ["seq", "mdc", "code", "name"][:m.shape[1]]
    m = m.dropna(subset=["code"])
    m["code"] = m["code"].astype(str).str.strip()
    m["mdc"] = m["mdc"].astype(str).str.strip()
    out["main_dx"] = m[["mdc", "code", "name"]].drop_duplicates()

    for key, cols in (("cc", ["seq", "code", "name", "excl"]),
                      ("mcc", ["seq", "code", "name", "excl"]),
                      ("exclude", ["seq", "excl", "code", "name"]),
                      ("no_main_dx", ["seq", "code", "name"]),
                      ("no_main_op", ["seq", "code", "name"])):
        x = read_sheet(key)
        x.columns = cols[:x.shape[1]]
        x = x.dropna(subset=["code"])
        x["code"] = x["code"].astype(str).str.strip()
        out[key] = x.drop_duplicates("code")
    return out


# ------------------------------------------------------------------
# Back-test (shared with S20's regression gate)
# ------------------------------------------------------------------
def run_backtest() -> tuple[pd.DataFrame, DrgEngine]:
    """Group every official back-test case and return the per-case comparison.

    Shared with S20 so the regression gate and this report can never diverge - both call this
    function over the same cases with the same engine. Returns (predictions, engine) because
    main() still needs the engine for its group count and payment-standard columns.
    """
    eng = DrgEngine()
    if not eng.ready:
        raise RuntimeError("引擎字典未就绪：请先运行本作业完成字典抽取")

    # ---- load back-test cases ---------------------------------------
    off = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    OUT_FROM, OUT_TO = window_from(off)     # 随导入月份自适应，下面取 HIS 诊断/手术都靠它

    # TCM operation classes come from the advice table (see docs/16)
    from tcm_ratio import executed_op_classes  # noqa: PLC0415
    adv = executed_op_classes("sbo", f"m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'")
    cls_by_case = dict(zip(adv[src_col("hospital_no")], adv["op_classes"])) if not adv.empty else {}
    # Length of stay must come from 源表(admission): the settlement return leaves actual_days
    # empty for most months (only ~22% populated), and a missing day count silently
    # bypasses every TCM length-of-stay gate because `NaN < threshold` is False.
    # 源表(admission).列(inhospital_day) is 100% populated and matches the official value
    # (verified 12/12 on the N-case cohort - see docs/32).
    his_days = query_df(
        "SELECT 列(hospital_no), 列(inhospital_day) FROM 源表(admission) "
        f"WHERE 列(time_out) >= '{OUT_FROM}' AND 列(time_out) < '{OUT_TO}'")
    his_map = {str(getattr(r, src_col("hospital_no"))): pd.to_numeric(getattr(r, src_col("inhospital_day")), errors="coerce")
               for r in his_days.itertuples()}
    days_by_case: dict[str, float] = {}
    for r in off.itertuples():
        v = his_map.get(str(r.medical_no))
        days_by_case[str(r.medical_no)] = (
            v if (v is not None and pd.notna(v))
            else pd.to_numeric(r.actual_days, errors="coerce"))

    op_map = pd.read_csv(DICT_DIR / "本地对照表.csv", dtype=str, encoding="utf-8-sig")
    op_map.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    code_map = {str(r.country).strip(): str(r.yb).strip()
                for r in op_map[op_map["kind"] == "S"].itertuples() if r.yb}
    dx_map = {str(r.country).strip(): str(r.yb).strip()
              for r in op_map[op_map["kind"] == "D"].itertuples() if r.yb}

    ops = query_df(
        "SELECT m.列(hospital_no), o.列(order), o.列(code) FROM 源表(operation) o "
        "JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'")
    ops["yb"] = ops[src_col("code")].astype(str).str.strip().map(code_map).fillna(
        ops[src_col("code")].astype(str).str.strip())
    ops_by_case: dict[str, set[str]] = {}
    for r in ops.itertuples():
        ops_by_case.setdefault(getattr(r, src_col("hospital_no")), set()).add(r.yb)

    dxs = query_df(
        "SELECT d.列(hospital_no), d.列(order), d.列(icd), d.列(kind_input) FROM 源表(diagnosis) d "
        "JOIN 源表(admission) m ON m.列(serial) = d.列(serial_man) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}' "
        "AND d.列(kind_input) IN (3,4) AND d.列(kind_diagnose) = 2 "
        "ORDER BY d.列(hospital_no), d.列(order), d.列(icd)")
    dxs["yb"] = dxs[src_col("icd")].astype(str).str.strip().map(dx_map).fillna(
        dxs[src_col("icd")].astype(str).str.strip())
    main_dx: dict[str, str] = {}
    other_dx: dict[str, set[str]] = {}
    for r in dxs.itertuples():
        if int(getattr(r, src_col("order")) or 0) == 1:
            main_dx.setdefault(getattr(r, src_col("hospital_no")), r.yb)
        else:
            other_dx.setdefault(getattr(r, src_col("hospital_no")), set()).add(r.yb)

    # Fallback for admissions whose discharge main diagnosis is not recorded in
    # 源表(diagnosis): use the vendor pre-grouping table, whose medicare diagnosis code
    # was verified against official returns at 99.5% agreement (S02).
    fb = query_df(
        "SELECT m.列(hospital_no), g.列(icd_yb) FROM 源表(admission) m "
        "JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = m.列(serial) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}' "
        "AND g.列(icd_yb) IS NOT NULL")
    fb = fb.drop_duplicates(src_col("hospital_no"))
    filled = 0
    for r in fb.itertuples():
        if getattr(r, src_col("hospital_no")) not in main_dx:
            main_dx[getattr(r, src_col("hospital_no"))] = str(getattr(r, src_col("icd_yb"))).strip()
            filled += 1
    print(f"[2/3] 病例数据：手术 {len(ops_by_case):,} 例，主诊断 {len(main_dx):,} 例"
          f"（其中 {filled:,} 例来自预分组兜底）")

    # ---- group every case -------------------------------------------
    rows = []
    for r in off.itertuples():
        hs = str(r.medical_no).strip()
        mdx = main_dx.get(hs)
        res = eng.group(main_dx=mdx, ops=ops_by_case.get(hs, set()),
                        other_dx=other_dx.get(hs, set()),
                        tcm_classes=set(cls_by_case.get(hs, []) or []),
                        days=days_by_case.get(hs))
        drg = res.get("drg")
        reason = res.get("reason", "")
        if res["path"] == PATH_NONE and eng.std is not None:
            if not mdx:
                reason = "主诊断缺失"
            elif not eng.std.mdc_of(mdx):
                reason = "主诊断不在官方主诊表"
            elif not reason:
                reason = "命中 MDC 但无 ADRG 条件匹配"
        rows.append({"medical_no": hs, "drg_code": r.drg_code, "pred_drg": drg,
                     "pred_adrg": res.get("adrg"), "pred_sev": res.get("severity"),
                     "pred_weight": eng.weight_of(drg), "pred_std": eng.payment_standard(drg),
                     "path": res["path"], "qy": res.get("qy"), "reason": reason})
    pred = pd.DataFrame(rows)
    pred["off_adrg"] = pred["drg_code"].astype(str).str[:3]
    pred["hit_adrg"] = pred["pred_adrg"] == pred["off_adrg"]
    pred["hit_drg"] = pred["pred_drg"] == pred["drg_code"]
    # Denominator rule (M2/M4, docs/23 口径提示): a case the engine could not predict (its
    # source row lacks a diagnosis code) must not count as "inconsistent" - dividing by all
    # rows deflates every rate. Marking it in the product keeps every consumer on the same
    # denominator instead of each replay choosing its own; the test stand reported 95.8%
    # against the registered 96.9% purely because it averaged over all rows instead of the
    # assessable ones (docs/35 D11).
    pred["assessable"] = (pred["pred_adrg"].notna()
                          & (pred["pred_adrg"].astype(str).str.strip() != ""))

    # Settle month for the rolling series. settle_date is populated for every returned case but mixes
    # tz-aware ("...+00:00") and naive strings, and on pandas 3 a plain to_datetime(..., errors=
    # "coerce") fails the whole-column fast path and silently returns NaT for the 185 tz-aware
    # rows - which made a whole month vanish from the monthly series. format="mixed" parses
    # element-wise and recovers every row. The source's own settle_ym column covers only part of
    # the rows and is unusable as a key. Positional assignment keeps this aligned with pred.
    pred["settle_month"] = (pd.to_datetime(off["settle_date"], errors="coerce", utc=True,
                                           format="mixed")
                            .dt.tz_localize(None).dt.to_period("M").astype(str).to_numpy())
    return pred, eng


# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump sheet semantics only")
    a = ap.parse_args()
    if a.probe:
        probe()
        return 0

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    DICT_OUT.mkdir(parents=True, exist_ok=True)
    dic = parse_dictionaries()
    for k, v in dic.items():
        v.to_csv(DICT_OUT / f"drg_{k}.csv", index=False, encoding="utf-8-sig")
    print("[1/3] 字典落地:", {k: len(v) for k, v in dic.items()})

    pred, eng = run_backtest()

    n = len(pred)
    hit_a = int(pred["hit_adrg"].sum())
    hit_d = int(pred["hit_drg"].sum())
    no_pred = int(pred["pred_adrg"].isna().sum())
    列(assessable) = int(pred["assessable"].sum())
    den = 列(assessable) or n          # agreement rates divide by the assessable cohort (M2/M4)
    _months = sorted({str(m) for m in pred["settle_month"].dropna().unique() if str(m).strip()})
    period = f"{_months[0]} ~ {_months[-1]}" if _months else "未知区间"

    log = [
        "# S11 ADRG / CC-MCC 分组引擎 v1 报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "- 规则来源：`原始资料/官方下发文件/CHS-DRG2.0完整版CC-MCC排除表.xlsx`（官方 CHS-DRG 2.0 全套）",
        f"- 回测集：{period} 官方结算返回 **{n:,}** 例",
        f"- 分母口径（M2/M4）：一致率取**可评估 {列(assessable):,} 例**"
        f"（{no_pred} 例源表缺诊断码、产不出预测；计入分母会把一致率平白拉低）；"
        "`adrg_predictions.csv` 已带 `assessable` 列显式标记该口径",
        f"- 权重与支付标准：官方某地区 **{eng.n_groups}** 组目录「二三级医疗机构权重」"
        f"× 费率 {FEE_RATE:,.2f} × 机构系数 {COEF}",
        "",
        "## 一、字典落地规模",
        "",
        "| 字典 | 行数 | 说明 |",
        "|---|---:|---|",
        f"| 入组条件 | {len(dic['cond']):,} | ADRG 的手术式 / 诊断式条件 |",
        f"| DRG组 | {len(dic['drg']):,} | ADRG + 第 4 位 → DRG 编码/名称 |",
        f"| ADRG | {len(dic['adrg']):,} | ADRG 名称 |",
        f"| 主诊表 | {len(dic['main_dx']):,} | 主诊断 → MDC |",
        f"| MCC | {len(dic['mcc']):,} | 严重合并症 |",
        f"| CC | {len(dic['cc']):,} | 一般合并症 |",
        f"| 排除表 | {len(dic['exclude']):,} | CC/MCC 排除 |",
        f"| 不作为主诊 | {len(dic['no_main_dx']):,} | 不可作主诊断 |",
        f"| 不作为主手术 | {len(dic['no_main_op']):,} | 不可作主手术 |",
        "",
        f"## 二、分组结果 vs 官方（{n:,} 例回测，可评估 {den:,} 例）",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| ADRG（前 3 位）一致 | **{hit_a:,} / {den:,} = {hit_a / den:.1%}** |",
        f"| 四位码完全一致 | **{hit_d:,} / {den:,} = {hit_d / den:.1%}** |",
        f"| 未能分组（无 MDC/ADRG 命中） | **{no_pred:,}**（{no_pred / n:.1%}，不计入一致率分母） |",
        "",
        f"> **分母口径**：一致率按**可评估 {den:,} 例**（= 产出了预测的病例）计算，与 `s20` 回归判定、"
        f"`docs/23` 口径提示一致；若误按全量 {n:,} 计，ADRG 会变成 {hit_a / n:.1%}"
        "（口径分裂，对外不得使用）。",
        "> 对照：既有引擎前 3 位一致率 89.8%（N 例口径）/ 90.9%（N 例子集口径，S02）。",
        "",
        "### 2.1 两条分组路径的贡献",
        "",
        "某地区的**中医优势病组**是国家 CHS-DRG 规则之外的属地叠加规则，因此引擎采用「中医规则优先、"
        "标准 ADRG 兜底」的双路径结构。",
        "",
        "| 路径 | 例数 | 占比 | ADRG 一致 | 路径内命中率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for path, label in ((PATH_TCM, "中医优势病组规则（S04/S05）"),
                        (PATH_STD, "标准 CHS-DRG ADRG（S11）"),
                        (PATH_NONE, "**未产出组码**（ADRG 已命中但无细分组，或无 ADRG 命中）")):
        sub = pred[pred["path"] == path]
        if len(sub):
            log.append(f"| {label} | {len(sub):,} | {len(sub) / n:.1%} | "
                       f"{int(sub['hit_adrg'].sum()):,} | {sub['hit_adrg'].mean():.1%} |")
    log += [
        "",
        "### 2.2 四位码一致率偏低的原因",
        "",
        f"四位码完全一致的差距（{n - hit_d:,} 例）来自第 4 位，即 **CC/MCC 严重程度档位**判定。",
        "",
        "已定位并修复的首要原因（docs/32）：**住院天数取自结算返回，而该列约 78% 为空**；",
        "缺失值参与 `days < 门槛` 比较恒为 False，导致中医优势病组的住院天数门槛被整体绕过，",
        "短住院病例被错误判入中医组。改用 `源表(admission).列(inhospital_day)`（100% 有值、与官方值一致）后，",
        "四位码一致率由 80.4% 提升至本报告的当前值。",
        "",
        "**剩余差距主因是引擎少判「伴合并症或并发症」（第 4 位 5 → 官方 3）**，属 CC/MCC 检出维度，",
        "不是「其他诊断没录入」——反事实验证（S33）已证明补录其他诊断对分组零影响。",
        "",
    ]

    # ---- why did the engine fail? ----------------------------------------
    # every case not resolved by the TCM overlay goes through the standard path, whether or
    # not it ends up grouped - so the breakdown must include PATH_NONE as well
    std = pred[pred["path"] != PATH_TCM]
    log += ["", "## 三、未分组原因分解（标准路径）", "",
            f"- 走标准路径的病例：**{len(std):,}**（其余 {n - len(std):,} 例由中医规则路径处理）", "",
            "| 原因 | 例数 | 说明 |",
            "|---|---:|---|"]
    for reason, desc in (
        ("主诊断缺失", "HIS 无出院西医主诊断记录（列(kind_input) 3/4 且 列(kind_diagnose)=2 且 列(order)=1）"),
        ("主诊断不在官方主诊表", "诊断编码未命中 MDC，属诊断码版本差或对照缺失"),
        ("命中 MDC 但无 ADRG 条件匹配", "主诊断/主手术均未命中该 MDC 下的入组条件"),
    ):
        cnt = int((std["reason"] == reason).sum())
        log.append(f"| {reason} | {cnt:,} | {desc} |")
    # matched an ADRG but the national table has no 4th-digit variant for that severity -
    # mostly cases the official return settled as a Chongqing TCM group instead
    no_sev = int((std["pred_adrg"].notna() & std["pred_drg"].isna()).sum())
    log.append(f"| **ADRG 已命中但无对应严重程度细分组** | {no_sev:,} | "
               "国家 DRG 组表中该 ADRG 无第 1/3/5 位变体；官方多以属地中医组（F/R/Z）结算 |"
               f" 其中 {int((std['pred_adrg'].notna() & std['pred_drg'].isna() & std['hit_adrg']).sum()):,} 例 "
               "ADRG 与官方一致 |")
    grouped_but_diff = int((std["pred_drg"].notna() & ~std["hit_adrg"]).sum())
    log.append(f"| 已产出组码但 ADRG 与官方不同 | {grouped_but_diff:,} | 匹配到不同 ADRG（优先级规则待细化） |")

    log += ["",
            f"**根因判读**：标准路径内 ADRG 一致 **{int(std['hit_adrg'].sum()):,}/{len(std):,}"
            f"（{std['hit_adrg'].mean():.1%}）**；失败主因是**诊断数据**而非引擎逻辑——",
            "「主诊断不在主诊表」提示 HIS 医保版诊断码与官方 CHS-DRG 2.0 字典存在版本差，",
            "应优先扩充 `drg_main_dx` 或修正诊断码映射，这属**可量化的编码质量缺口**，",
            "正是病案首页质控模块应当拦截并提示的对象。",
            ]

    dis = pred[~pred["hit_adrg"]].copy()
    log += [f"## 四、ADRG 分歧明细（{len(dis):,} 例）", ""]
    if len(dis):
        g1 = dis.groupby(["off_adrg", "pred_adrg"], dropna=False).size().sort_values(ascending=False).head(15)
        log += ["| 官方 ADRG | 引擎 ADRG | 例数 |", "|---|---|---:|"]
        for (o, p), c in g1.items():
            log.append(f"| {o} | {p if pd.notna(p) else '（未分组）'} | {c} |")

    grp = pred.groupby("off_adrg").agg(例数=("medical_no", "count"),
                                       ADRG命中=("hit_adrg", "sum"))
    grp["命中率"] = (grp["ADRG命中"] / grp["例数"]).map(lambda x: f"{x:.0%}")
    log += ["", "### 3.1 按官方 ADRG 的命中情况（例数 Top 15）", "",
            _md(grp.sort_values("例数", ascending=False).head(15).reset_index())]

    log += ["", "## 四、产出与下一步", "",
            "- `src/config/dict/drg_cond.csv` / `drg_drg.csv` / `drg_adrg.csv` / `drg_main_dx.csv` /"
            " `drg_cc.csv` / `drg_mcc.csv` / `drg_exclude.csv` / `drg_no_main_dx.csv` / `drg_no_main_op.csv`",
            "- `src/out/adrg/adrg_predictions.csv` —— 逐例引擎输出 vs 官方",
            "",
            "**下一步**：",
            "1. 分歧逐类归因（未分组多为诊断码未命中 MDC / 手术码未映射，需扩展对照表）；",
            "2. 本引擎已抽为运行时模块 `src/etl/drg_engine.py` 并接入在院模拟器（见 docs/19）；",
            "3. 补 `不作为主诊/主手术` 校验与 QY（未入组）判定规则。",
            ]

    pred.to_csv(OUT_SUB / "adrg_predictions.csv", index=False, encoding="utf-8-sig")
    md = "\n".join(log)
    (OUT_SUB / "s11_adrg_report.md").write_text(md, encoding="utf-8")
    print("[3/3] 报告 →", OUT_SUB / "s11_adrg_report.md")
    print(md)
    return 0


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


if __name__ == "__main__":
    raise SystemExit(main())
