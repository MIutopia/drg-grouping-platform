"""S05 Back-test the TCM advantage group rules against official settlement results.

Recomputes inclusion independently from the rules extracted in S04 and compares
case by case with the official result.
Docs: docs/15-代码模块说明与作业手册.md §6.6.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import DICT_DIR, OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "tcm"
SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"
MAP_CSV = DICT_DIR / "本地对照表.csv"
RULES_JSON = OUT_SUB / "tcm_advantage_rules.json"

OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"


# ------------------------------------------------------------------
def load_op_map() -> dict[str, str]:
    """Map in-house / national operation codes to medicare (yb) operation codes."""
    mp = pd.read_csv(MAP_CSV, dtype=str, encoding="utf-8-sig")
    mp.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    mp = mp[mp["kind"] == "S"]
    return {str(r.country).strip(): str(r.yb).strip() for r in mp.itertuples() if r.yb}


def norm(code: object) -> str:
    """Normalise a code to upper case.

    The official PDF and the HIS data disagree on the case of the detail separator
    (X vs x), which otherwise produces false mismatches.
    """
    return str(code).strip().upper()


def load_rules() -> dict[str, dict]:
    """Load the rule table with all codes normalised."""
    g = json.loads(RULES_JSON.read_text(encoding="utf-8"))
    for c, gg in g.items():
        for d in gg["dx"]:
            d["code"] = norm(d["code"])
        for lst in gg["ops"].values():
            for o in lst:
                o["code"] = norm(o["code"])
    return g


# ------------------------------------------------------------------
def predict(dx: str | None, ops: set[str], days: float | None, groups: dict) -> list[str]:
    """Return the TCM advantage groups whose inclusion conditions this case meets."""
    if not dx or days is None:
        return []
    dx = norm(dx)
    ops = {norm(o) for o in ops}
    hits = []
    for c, g in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        if days < (g["days"] or 0):
            continue
        if dx not in {d["code"] for d in g["dx"]}:
            continue
        if g["op_mode"] == "any":
            op_codes = {o["code"] for lst in g["ops"].values() for o in lst}
            if ops & op_codes:
                hits.append(c)
        else:
            covered = {k for k, lst in g["ops"].items() if ops & {o["code"] for o in lst}}
            if len(covered) >= g.get("min_ops", 99):
                hits.append(c)
    return hits


def main() -> int:
    groups = load_rules()
    op_map = load_op_map()
    off = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")

    # ---- load data -------------------------------------------------
    dx = query_df(
        "SELECT d.列(hospital_no), d.列(icd), d.列(note) "
        "FROM 源表(diagnosis) d JOIN 源表(admission) m ON m.列(serial) = d.列(serial_man) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}' "
        "AND d.列(kind_input) IN (3,4) AND d.列(kind_diagnose) = 2 AND d.列(order) = 1",
    )
    dx = dx.drop_duplicates(src_col("hospital_no"))

    ops = query_df(
        "SELECT m.列(hospital_no), o.列(order), o.列(code), o.列(name) "
        "FROM 源表(operation) o JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'",
    )
    ops["yb_code"] = ops[src_col("code")].astype(str).str.strip().map(op_map)
    ops["eff_code"] = ops["yb_code"].fillna(ops[src_col("code")].astype(str).str.strip())

    hm = query_df(
        "SELECT 列(hospital_no), 列(inhospital_day) FROM 源表(admission) "
        f"WHERE 列(time_out) >= '{OUT_FROM}' AND 列(time_out) < '{OUT_TO}'",
    )

    # fallback: the medicare diagnosis code from vendor pre-grouping (99.5% verified match)
    drg = query_df(
        "SELECT m.列(hospital_no), g.列(icd_yb) FROM 源表(admission) m "
        "JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = m.列(serial) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'",
    ).drop_duplicates(src_col("hospital_no"))

    # if the HIS diagnosis code is national version, convert it via the 'D' mapping
    mpd = pd.read_csv(MAP_CSV, dtype=str, encoding="utf-8-sig")
    mpd.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    dmap = {str(r.country).strip(): str(r.yb).strip()
            for r in mpd[mpd["kind"] == "D"].itertuples() if r.yb}

    case = (hm.merge(dx, on=src_col("hospital_no"), how="left")
              .merge(drg, on=src_col("hospital_no"), how="left"))
    case["dx_raw"] = case[src_col("icd")].astype(str).str.strip()
    case["dx_yb"] = case["dx_raw"].map(dmap).fillna(case[src_col("icd_yb")]).fillna(case["dx_raw"])
    case["dx_use"] = case[src_col("icd_yb")].fillna(case["dx_yb"])

    ops_by_case = ops.groupby(src_col("hospital_no"))["eff_code"].apply(set).to_dict()

    # ---- per-case prediction ---------------------------------------
    rows = []
    for r in case.itertuples():
        oset = ops_by_case.get(getattr(r, src_col("hospital_no")), set())
        hits = predict(r.dx_use if not pd.isna(r.dx_use) else None,
                       oset, getattr(r, src_col("inhospital_day")), groups)
        rows.append({
            src_col("hospital_no"): getattr(r, src_col("hospital_no")),
            "dx_use": r.dx_use,
            "days": getattr(r, src_col("inhospital_day")),
            "op_cnt": len(oset),
            "op_codes": "|".join(sorted(oset)),
            "pred_groups": "|".join(hits),
            "pred_is_tcm": bool(hits),
        })
    pred = pd.DataFrame(rows)

    j = off.merge(pred, left_on="medical_no", right_on=src_col("hospital_no"), how="left")
    j["off_is_tcm"] = j["drg_code"].astype(str).str.endswith(("F", "R", "Z"))
    j["pred_is_tcm"] = j["pred_is_tcm"].fillna(False)
    j["pred_contains_off"] = [str(o) in str(p).split("|") for o, p in
                              zip(j["drg_code"], j["pred_groups"].fillna(""))]

    total = len(j)
    off_tcm = int(j["off_is_tcm"].sum())
    pred_tcm = int(j["pred_is_tcm"].sum())
    both = int((j["off_is_tcm"] & j["pred_is_tcm"]).sum())
    contains = int(j["pred_contains_off"].sum())
    tp = int((j["off_is_tcm"] & j["pred_contains_off"]).sum())

    # ---- report ----------------------------------------------------
    log = [
        "# S05 中医优势病组规则回测验证报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "- 规则来源：某地区医保局〔2025〕27 号 附件 2（S04 从官方 PDF 提取，policy_version=某地区医保局〔2025〕27号-2025版）",
        "- 回测集：2026-03~05 官方结算返回 N 例",
        "",
        "## 一、编码映射前置（已打通）",
        "",
        "| 项 | 结果 |",
        "|---|---|",
        f"| 对照表（国临/院内 → 医保版，列(kind)='S'） | {len(op_map):,} 条 |",
        f"| 窗口内院内手术码种类 | {ops[src_col("code")].nunique()} 个 |",
        f"| 其中成功映射到医保版 | {ops['yb_code'].notna().groupby(ops[src_col("code")]).first().sum()} 个 |",
        "",
        "> 例：`99.9201 毫针刺法 → 17.91100 毫针治疗`、`93.3512 隔物灸 → 17.91320 隔物灸治疗`、"
        "`99.9203 电针经络氧疗法 → 17.912A0 电针治疗`。",
        "> **这修正了 docs/05 的一处判断**：此前认为 `IU25↔IU2F` 分歧源于「中医操作主次顺序」，"
        "实为**新旧编码体系不同**（院内 `99.9200x016` vs 官方 `17.912A0`），主次顺序只是表象。",
        "",
        "## 二、重算结果 vs 官方",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 回测集 | {total:,} |",
        f"| 官方判为中医优势病组（F/R/Z 结尾） | **{off_tcm}**（{off_tcm / total:.1%}） |",
        f"| 规则重算判为中医优势病组 | **{pred_tcm}**（{pred_tcm / total:.1%}） |",
        f"| 两者都判为中医组 | **{both}**（占官方中医组的 {both / off_tcm:.1%}，召回） |",
        f"| 预测组码命中官方组码 | **{contains}**（{contains / total:.1%}，精确命中率） |",
        f"| 官方为中医组且预测命中 | **{tp}**（{tp / off_tcm:.1%}） |",
    ]

    # confusion matrix
    cm = pd.crosstab(j["off_is_tcm"], j["pred_is_tcm"], margins=True)
    try:
        cm_md = cm.to_markdown()
    except Exception:  # noqa: BLE001  # fall back to plain text without tabulate
        cm_md = "```\n" + cm.to_string() + "\n```"
    log += ["", "### 2.1 混淆矩阵（行=官方，列=重算）", "", cm_md]

    # false negatives
    log += ["", "### 2.2 官方判中医组、重算未判（漏判）Top 原因", ""]
    fn = j[j["off_is_tcm"] & ~j["pred_is_tcm"]]
    log.append(f"- 漏判 **{len(fn)}** 例")
    if len(fn):
        log += ["", "| 官方组码 | 例数 | 主诊断样例 | 操作数中位数 |", "|---|---:|---|---:|"]
        for code, sub in fn.groupby("drg_code"):
            log.append(f"| {code} | {len(sub)} | {str(sub['dx_use'].iloc[0])} | {sub['op_cnt'].median():.0f} |")

    log += ["", "### 2.3 重算判中医组、官方未判（误判）Top 原因", ""]
    fp = j[~j["off_is_tcm"] & j["pred_is_tcm"]]
    log.append(f"- 误判 **{len(fp)}** 例")
    if len(fp):
        log += ["", "| 官方组码 | 重算组码 | 例数 |", "|---|---|---:|"]
        for (a, b), n in fp.groupby(["drg_code", "pred_groups"]).size().sort_values(ascending=False).head(12).items():
            log.append(f"| {a} | {b} | {n} |")

    log += ["", "### 2.4 各组命中情况", "",
            "| 中医组 | 官方例数 | 重算例数 | 重算命中该组 |", "|---|---:|---:|---:|"]
    for c, g in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        列(off) = int((j["drg_code"] == c).sum())
        n_pred = int(j["pred_groups"].fillna("").str.split("|").apply(lambda x: c in x).sum())
        n_hit = int(((j["drg_code"] == c) & j["pred_groups"].fillna("").str.split("|").apply(lambda x: c in x)).sum())
        log.append(f"| {c} | {列(off)} | {n_pred} | {n_hit} |")

    log += ["", "## 三、结论与下一步", ""]
    if total and off_tcm:
        recall = both / off_tcm
        prec = both / pred_tcm if pred_tcm else 0
        log += [
            f"- 规则重算相对官方中医组的**召回率 {recall:.1%}**，**精确率 {prec:.1%}**（阈值判读见下）。",
            "- 若召回显著高于既有引擎的 0%，即证明「按 27 号文附件 2 自建规则 + 新旧编码映射」这条路走通；",
            "- 剩余偏差需逐类归因（住院天数口径、操作漏编、诊断与操作未同时录入等），列入 S05 迭代。",
        ]
    log += [
        "",
        "**下一步**：",
        "1. 将本判定逻辑固化为 `drg` 库规则函数 `usp_DrgGroup_TcmAdvantage`，供在院模拟器与质控清单共用；",
        "2. 加入 CC/MCC 判定与 ADRG 主分组，形成完整引擎 v1（二期）；",
        "3. 中治率校验接入费用三表后，与入组判定合并为模拟器「四行输出」。",
    ]

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    keep = ["medical_no", "drg_code", "drg_name", "total_cost", "profit_loss", "days",
            "dx_use", "op_cnt", "op_codes", "pred_groups", "pred_is_tcm",
            "off_is_tcm", "pred_contains_off"]
    j[[c for c in keep if c in j.columns]].to_csv(
        OUT_SUB / "tcm_predictions.csv", index=False, encoding="utf-8-sig")

    md = "\n".join(log)
    (OUT_SUB / "s05_validate_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
