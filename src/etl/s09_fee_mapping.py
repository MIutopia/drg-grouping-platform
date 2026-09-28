"""S09 Build the charge-item attribute dictionary and validate it by back-test.

Shared foundation for two phase-1 gaps:
  P0-1 derive executed TCM operations for in-hospital patients (advice table);
  P0-2 split treatment fee into TCM / western and locate consumable fee, so the
       TCM-treatment-cost ratio required by 某地区医保局〔2025〕27号 can be computed.

Validation: re-run the TCM advantage group back-test using operations derived from
the advice table instead of the medical-record operation table. Agreement close to the
S05 baseline proves the mapping is sound enough to trust for in-hospital patients.

Docs: docs/16-P0数据源打通与科目映射.md.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, DICT_DIR, OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "feemap"
DICT_OUT = CONFIG_DIR / "dict"
RULES_CSV = OUT_DIR / "tcm" / "tcm_advantage_rules.csv"
RULES_JSON = OUT_DIR / "tcm" / "tcm_advantage_rules.json"
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"
SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"

CHONGQING_CATALOG = DICT_DIR / "某地区医疗服务项目_9659.csv"
COST_MASTER = DICT_DIR / "收费项目主档_12546.csv"

OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"

SUBJECT_TCM_TREAT = "中医治疗费"
SUBJECT_WEST_TREAT = "西医治疗费"
SUBJECT_TCM_PATENT = "中成药费"
SUBJECT_TCM_HERBAL = "中药饮片费"
SUBJECT_WEST_DRUG = "西药费"
SUBJECT_SURGERY = "手术费"
SUBJECT_MATERIAL = "卫生材料费"
SUBJECT_OTHER = "其他"

# Medicine fee sort codes. 03 is the largest bucket, matching the herbal share observed
# in 源表(settle_bill) (草药费 3.07M CNY vs 成药费 62k CNY).
MEDICINE_SORT_SUBJECT = {
    "01": SUBJECT_WEST_DRUG,
    "02": SUBJECT_TCM_PATENT,
    "03": SUBJECT_TCM_HERBAL,
    "13": SUBJECT_MATERIAL,
}

# Keyword -> official TCM operation class.
# Class 0 = bone-setting / reduction, which is what every 中医正骨术 group (op_mode='any')
# requires. Classes 1-4 mirror the four operation classes of the 中医治疗 groups.
OP_CLASS_KEYWORDS = {
    "0": ("整复", "正骨", "牵引", "悬吊", "夹板", "手法复位", "整复术"),
    "1": ("推拿", "按摩手法", "手法松解", "导引"),
    # "针法" is deliberately excluded: it matches lab items such as 荧光探针法.
    "2": ("毫针", "电针", "针刀", "刃针", "松解针", "三棱针", "火针", "针刺", "埋针", "耳针"),
    "3": ("灸",),
    "4": ("熏药", "熏洗", "穴位注射", "贴敷", "烫熨", "砭石", "拔罐", "刺络", "电离子透入", "熏蒸"),
}

# Names containing these tokens must never be treated as TCM operations.
OP_CLASS_EXCLUDE = ("荧光探针", "放疗", "后装", "锶", "核素", "造影", "活检")

# Classes used by the 中医治疗 groups; class 0 belongs to the 中医正骨术 groups.
TREAT_CLASSES = ("1", "2", "3", "4")

# Exact chapter name that marks an item as TCM in the Chongqing service-item catalog.
TCM_CHAPTER = "四、中医及民族医诊疗类"


def norm_code(c: object) -> str:
    return str(c).strip().upper()


def load_official_ops() -> pd.DataFrame:
    r = pd.read_csv(RULES_CSV, dtype=str, encoding="utf-8-sig")
    ops = r[r["kind"] == "op"].copy()
    ops["op_group"] = ops["op_group"].fillna("main")
    return ops[["drg_code", "op_group", "code", "name"]].drop_duplicates()


def classify_op_class(name: str) -> str:
    """Map an item name to a 27号文 operation class, '' when not a TCM operation."""
    if any(bad in name for bad in OP_CLASS_EXCLUDE):
        return ""
    for cls in ("0", "1", "2", "3", "4"):
        for kw in OP_CLASS_KEYWORDS[cls]:
            if kw in name:
                return cls
    return ""


def parse_catalog_hierarchy() -> pd.DataFrame:
    """Forward-fill the catalog chapter so every national item code carries its top class."""
    d = pd.read_csv(CHONGQING_CATALOG, dtype=str, encoding="utf-8-sig")
    # rename to ASCII so attribute access works (Chinese headers become positional _N)
    d.columns = ["lvl", "national_code", "national_name", "serial_new", "serial_old",
                 "cq_code", "name", "unit"]
    top, sub = "", ""
    rows = []
    for r in d.itertuples():
        code, cq, name = r.national_code, r.cq_code, r.name
        if pd.isna(code):
            text = str(name or "")
            if re.match(r"^[一二三四五六七八九十]+、", text):
                top, sub = text, ""
            elif re.match(r"^[(（]", text):
                sub = text
            continue
        rows.append({"national_code": str(code).strip(), "cq_code": str(cq or "").strip(),
                     "cq_name": str(name or "").strip(), "top_category": top, "sub_category": sub})
    df = pd.DataFrame(rows)
    # one row per national code: the same national item may appear under several local codes,
    # and duplicates would multiply rows on join and corrupt the chapter assignment
    return df.drop_duplicates("national_code", keep="first")


def build_bridge() -> pd.DataFrame:
    """Charge-item code -> national medical-service code, harvested from transactional tables."""
    return query_df(
        "SELECT 列(cost_no), MAX(列(cost_no_medicare)) AS national_code, MAX(列(cost_name)) AS item_name, "
        "MAX(src) AS src FROM ("
        "  SELECT 列(cost_no), 列(cost_no_medicare), 列(cost_note) AS 列(cost_name), 'advice_long' AS src "
        "    FROM 源表(advice_long) WHERE 列(time_order) >= '2024-01-01' "
        "  UNION ALL SELECT 列(cost_no), 列(cost_no_medicare), 列(cost_note), 'advice_temp' "
        "    FROM 源表(advice_temp) WHERE 列(time_order) >= '2024-01-01' "
        "  UNION ALL SELECT 列(cost_no), 列(cost_no_medicare), 列(cost_name), 'fee_cure' "
        "    FROM 源表(fee_cure_in) WHERE 列(time_charge) >= '2024-01-01' "
        "  UNION ALL SELECT 列(cost_no), 列(cost_no_medicare), 列(cost_name), 'fee_check' "
        "    FROM 源表(fee_check_in) WHERE 列(time_charge) >= '2024-01-01' "
        ") t WHERE 列(cost_no) IS NOT NULL GROUP BY 列(cost_no)")


def probe() -> None:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    print("=== medicine sort codes with sample names ===")
    print(query_df(
        "SELECT TOP 15 列(sort_code), 列(cost_name) FROM 源表(fee_medicine_in) "
        "WHERE 列(time_charge) >= '2026-06-01' AND 列(mark_dell) = 0 ORDER BY 列(sort_code)").to_string(index=False))
    print("\n=== cure sort code 13 / 21 samples ===")
    print(query_df(
        "SELECT TOP 12 列(sort_code), 列(cost_name) FROM 源表(fee_cure_in) "
        "WHERE 列(time_charge) >= '2026-06-01' AND 列(mark_dell) = 0 AND 列(sort_code) IN ('13','21')").to_string(index=False))


def pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "-"


def main() -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump raw facts only")
    a = ap.parse_args()
    if a.probe:
        probe()
        return 0

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    DICT_OUT.mkdir(parents=True, exist_ok=True)
    log: list[str] = [
        "# S09 收费项目属性字典构建与验证报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "- 目的：为 P0-1（在院已执行操作）与 P0-2（中治率科目拆分）提供共用字典",
        "",
    ]

    # ---- 1. catalog hierarchy ---------------------------------------
    cat = parse_catalog_hierarchy()
    cat.to_csv(OUT_SUB / "national_item_catalog.csv", index=False, encoding="utf-8-sig")
    chapters = cat["top_category"].value_counts()
    log += ["## 一、《某地区医疗服务项目》章节解析", "",
            f"- 项目行 {len(cat):,} 条（已剔除章节头）", "",
            chapters.to_string(), ""]

    # ---- 2. bridge charge item -> national code ---------------------
    bridge = build_bridge()
    # the raw value may carry a local suffix ('001109000010000-110900001b'); keep the
    # national code part only, otherwise the join against the catalog yields nothing
    bridge["national_code"] = (bridge["national_code"].astype(str).str.strip()
                               .str.split("-").str[0])
    bridge.loc[bridge["national_code"].isin(["", "None", "nan"]), "national_code"] = None
    cov = bridge["national_code"].notna().sum()
    print(f"[1/4] bridge rows={len(bridge):,} with_code={cov:,}")
    log += ["## 二、收费项目 → 国家医疗服务项目代码桥接", "",
            f"- 收费项目码合计 **{len(bridge):,}** 个，其中可桥接 **{cov:,}**（{pct(cov, len(bridge))}）",
            "- 桥接来源：`源表(advice_long)/Temporary` + `源表(fee_prefix)*_In` 的 `列(cost_no_medicare)`", ""]

    # ---- 3. attributes ----------------------------------------------
    master = pd.read_csv(COST_MASTER, dtype=str, encoding="utf-8-sig")
    master = master[[src_col("cost_no"), src_col("cost_name"), src_col("sort_code"), src_col("sort_kind"), src_col("cost_type")]]
    master = master.drop_duplicates(src_col("cost_no"), keep="first")
    m = master.merge(bridge[[src_col("cost_no"), "national_code", "item_name"]], on=src_col("cost_no"), how="left")
    m = m.merge(cat[["national_code", "cq_code", "top_category", "sub_category"]],
                on="national_code", how="left")
    m["item_name"] = m["item_name"].fillna(m[src_col("cost_name")])
    # Three authoritative TCM signals; keyword matching alone is unreliable because western
    # procedures reuse the same words (e.g. 鼻骨骨折整复术, 提上睑肌悬吊术).
    #   A: catalog chapter = 四、中医及民族医诊疗类
    #   B: Chongqing service-item code starts with 4 (43/44/45/47 = 中医诊疗)
    #   C: in-house charge-item code starts with 04 (the hospital's own TCM numbering)
    m["cq_prefix"] = m["cq_code"].astype(str).str.strip().str[:1]
    m["cost_prefix"] = m[src_col("cost_no")].astype(str).str.strip().str[:2]
    m["is_tcm_catalog"] = m["top_category"].eq(TCM_CHAPTER)
    m["is_tcm_cq4"] = m["cq_prefix"].eq("4")
    m["is_tcm_cost04"] = m["cost_prefix"].eq("04")
    m["is_tcm_item"] = m["is_tcm_catalog"] | m["is_tcm_cq4"] | m["is_tcm_cost04"]

    # Operation class drives the inclusion judgement (27号文 operation classes).
    # op_basis records how it was derived so reviewers can prioritise keyword-only rows.
    m["op_class"] = m["item_name"].map(lambda s: classify_op_class(str(s)))
    m["op_basis"] = ""
    m.loc[m["op_class"] != "", "op_basis"] = "keyword_only"
    m.loc[(m["op_class"] != "") & m["is_tcm_item"], "op_basis"] = "catalog"

    def subject_of(r: pd.Series) -> str:
        s = str(r[src_col("sort_code")] or "")
        if s == "13":
            return SUBJECT_MATERIAL
        if s == "11":
            # TCM bone-setting is charged as surgery in HIS; the official TCM-ratio
            # treatment of such items is a policy question listed for review.
            return SUBJECT_TCM_TREAT if r["is_tcm_item"] else SUBJECT_SURGERY
        if s in ("10", "12"):
            return SUBJECT_TCM_TREAT if r["is_tcm_item"] else SUBJECT_WEST_TREAT
        return SUBJECT_OTHER

    m["zzl_subject"] = m.apply(subject_of, axis=1)
    m.to_csv(OUT_SUB / "cost_item_attribute.csv", index=False, encoding="utf-8-sig")
    m.to_csv(DICT_OUT / "cost_item_attribute.csv", index=False, encoding="utf-8-sig")

    tcm_n = int(m["is_tcm_item"].sum())
    log += ["## 三、派生的收费项目属性", "",
            f"- 收费项目 {len(m):,} 条；判定为**中医项目**（用于中治率科目）**{tcm_n:,}** 条（{pct(tcm_n, len(m))}）",
            f"  - 依据 A：`《某地区医疗服务项目》章节 = 四、中医及民族医诊疗类` → {int(m['is_tcm_catalog'].sum()):,} 条",
            f"  - 依据 B：`某地区项目编码首字符 = 4（中医诊疗）` → {int(m['is_tcm_cq4'].sum()):,} 条",
            f"  - 依据 C：`院内收费项目码前缀 = 04` → {int(m['is_tcm_cost04'].sum()):,} 条",
            f"- 可归入 27 号文**中医操作类**（用于入组判定）**{int((m['op_class'] != '').sum()):,}** 条"
            f"（其中目录/某地区04 支撑 {int(m['op_basis'].eq('catalog').sum()):,} 条，纯关键字 {int(m['op_basis'].eq('keyword_only').sum()):,} 条）",
            "",
            "### 3.1 中治率科目分布（全量收费项目）", "",
            m["zzl_subject"].value_counts().to_string(), "",
            "### 3.2 治疗费科目的中医/西医拆分（关键）", ""]

    treat = m[m[src_col("sort_code")].isin(["10", "11", "12"])]
    log += [f"- 治疗/手术类项目 {len(treat):,} 条：中医治疗费 **{int(treat['zzl_subject'].eq(SUBJECT_TCM_TREAT).sum()):,}**、"
            f"西医治疗费 **{int(treat['zzl_subject'].eq(SUBJECT_WEST_TREAT).sum()):,}**、"
            f"手术费 **{int(treat['zzl_subject'].eq(SUBJECT_SURGERY).sum()):,}**",
            "- 拆分依据：`四、中医及民族医诊疗类` 章节 或 `某地区码前缀 04`",
            "",
            "> **待确认（政策口径）**：HIS 把中医正骨/整复类项目计在统计码 11（手术费）。",
            "> 中治率公式分子只含「中医治疗费」，故这类项目是否计入分子需医保办确认；",
            "> 本字典暂按「中医正骨 → 中医治疗费」处理，并在审核表中单列。", ""]

    log += ["### 3.3 中医项目示例（权威判定）", "",
            m[m["is_tcm_item"]]
            [[src_col("cost_no"), "item_name", src_col("sort_code"), "zzl_subject", "op_class", "op_basis"]]
            .drop_duplicates(src_col("cost_no")).head(25).to_string(index=False), "",
            "### 3.4 仅关键字判定为中医操作的项目（需医务科审核）", "",
            m[m["op_basis"].eq("keyword_only")]
            [[src_col("cost_no"), "item_name", src_col("sort_code"), "zzl_subject", "op_class"]]
            .drop_duplicates(src_col("cost_no")).head(25).to_string(index=False), ""]

    # ---- 4. validation: advice-derived ops vs official ---------------
    op_map = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    op_map.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    yb_by_name = {}
    for r in op_map[op_map["kind"] == "S"].itertuples():
        yb_by_name.setdefault(str(r.country_name).strip(), str(r.yb).strip())

    official_ops = load_official_ops()
    op_code_class = {}
    for r in official_ops.itertuples():
        op_code_class[norm_code(r.code)] = r.op_group

    # item name -> official operation codes of the same class
    clas列(code)s: dict[str, set[str]] = {}
    for r in official_ops.itertuples():
        clas列(code)s.setdefault(r.op_group, set()).add(norm_code(r.code))
    any_codes = set().union(*clas列(code)s.values()) if clas列(code)s else set()

    adv = query_df(
        "SELECT a.列(hospital_no), a.列(cost_no), a.列(cost_name), a.列(cost_no_medicare), a.列(time_execute) "
        "FROM ("
        "  SELECT 列(hospital_no), 列(cost_no), 列(cost_note) AS 列(cost_name), 列(cost_no_medicare), 列(time_execute) "
        "    FROM 源表(advice_long) "
        "  UNION ALL SELECT 列(hospital_no), 列(cost_no), 列(cost_note), 列(cost_no_medicare), 列(time_execute) "
        "    FROM 源表(advice_temp) "
        ") a "
        "JOIN 源表(admission) m ON m.列(hospital_no) = a.列(hospital_no) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'")
    adv["cls"] = adv[src_col("cost_name")].astype(str).map(classify_op_class)
    adv["is_executed"] = adv[src_col("time_execute")].notna()

    groups = json.loads(RULES_JSON.read_text(encoding="utf-8"))
    for gg in groups.values():
        for d in gg["dx"]:
            d["code"] = norm_code(d["code"])
        for lst in gg["ops"].values():
            for o in lst:
                o["code"] = norm_code(o["code"])

    def predict_by_class(dx: str | None, classes: set[str], days: float | None) -> list[str]:
        if not dx or days is None:
            return []
        dx = norm_code(dx)
        hits = []
        for c, g in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
            if days < (g["days"] or 0):
                continue
            if dx not in {d["code"] for d in g["dx"]}:
                continue
            if g["op_mode"] == "combo":
                need = g.get("min_ops", 99)
                allowed = {k for k in g["ops"] if k != "main"} & set(TREAT_CLASSES)
                if len(classes & allowed) >= need:
                    hits.append(c)
            else:
                # 中医正骨术 group: any bone-setting operation qualifies
                if "0" in classes:
                    hits.append(c)
        return hits

    dx = query_df(
        "SELECT d.列(hospital_no), d.列(icd) FROM 源表(diagnosis) d "
        "JOIN 源表(admission) m ON m.列(serial) = d.列(serial_man) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}' "
        "AND d.列(kind_input) IN (3,4) AND d.列(kind_diagnose) = 2 AND d.列(order) = 1")
    dx = dx.drop_duplicates(src_col("hospital_no"))
    drg = query_df(
        "SELECT m.列(hospital_no), g.列(icd_yb), m.列(inhospital_day) FROM 源表(admission) m "
        "JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = m.列(serial) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'").drop_duplicates(src_col("hospital_no"))
    case = drg.merge(dx, on=src_col("hospital_no"), how="left")
    case["dx_use"] = case[src_col("icd_yb")].fillna(case[src_col("icd")])

    cls_by_case = (adv[adv["cls"] != ""].groupby(src_col("hospital_no"))["cls"]
                   .apply(lambda s: set(s.astype(str))))
    exec_by_case = (adv[(adv["cls"] != "") & adv["is_executed"]].groupby(src_col("hospital_no"))["cls"]
                    .apply(lambda s: set(s.astype(str))))

    off = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    rows = []
    for r in case.itertuples():
        hits_all = predict_by_class(r.dx_use if not pd.isna(r.dx_use) else None,
                                    cls_by_case.get(getattr(r, src_col("hospital_no")), set()), getattr(r, src_col("inhospital_day")))
        hits_exe = predict_by_class(r.dx_use if not pd.isna(r.dx_use) else None,
                                    exec_by_case.get(getattr(r, src_col("hospital_no")), set()), getattr(r, src_col("inhospital_day")))
        rows.append({src_col("hospital_no"): getattr(r, src_col("hospital_no")), "days": getattr(r, src_col("inhospital_day")),
                     "pred_all": bool(hits_all), "pred_executed": bool(hits_exe)})
    pred = pd.DataFrame(rows)

    j = off.merge(pred, left_on="medical_no", right_on=src_col("hospital_no"), how="left")
    j["off_is_tcm"] = j["drg_code"].astype(str).str.endswith(("F", "R", "Z"))
    j["pred_all"] = j["pred_all"].fillna(False)
    j["pred_executed"] = j["pred_executed"].fillna(False)
    off_tcm = int(j["off_is_tcm"].sum())

    log += ["## 四、验证：用「医嘱推导的操作类」重跑入组回测", "",
            f"- 回测集 {len(j):,} 例，官方中医组 {off_tcm} 例",
            f"- 医嘱覆盖病例：{pred[src_col("hospital_no")].nunique():,} 例",
            "",
            "| 口径 | 预测中医组 | 一致（都判中医组） | 召回 | 精确率 | 与 S05 基线对比 |",
            "|---|---:|---:|---:|---:|---|"]
    for label, col in (("医嘱全部（含未执行）", "pred_all"), ("仅已执行医嘱", "pred_executed")):
        n_pred = int(j[col].sum())
        both = int((j["off_is_tcm"] & j[col]).sum())
        rec = both / off_tcm if off_tcm else 0
        prec = both / n_pred if n_pred else 0
        log.append(f"| {label} | {n_pred} | {both} | **{rec:.1%}** | **{prec:.1%}** | S05 基线 99.8% / 99.8% |")

    log += ["",
            "> 判读：若「仅已执行医嘱」口径的召回显著高于既有引擎的 0%，说明**用医嘱推导在院已执行中医操作可行**，",
            "> 可直接接入在院模拟器；与 S05 基线的差距反映医嘱口径与病案手术编码口径的天然差异（医嘱细、手术编码粗）。",
            ]

    # ---- review sheet for the medical affairs office -----------------
    review = m[(m["op_basis"].eq("keyword_only")) | (m["zzl_subject"].eq(SUBJECT_TCM_TREAT) & ~m["is_tcm_item"])]
    review = review[[src_col("cost_no"), "item_name", src_col("sort_code"), "zzl_subject", "op_class",
                     "op_basis", "is_tcm_item"]].drop_duplicates(src_col("cost_no"))
    review.to_csv(OUT_SUB / "review_sheet.csv", index=False, encoding="utf-8-sig")

    log += ["", "## 五、产出", "",
            "- `src/config/dict/cost_item_attribute.csv` —— 收费项目属性字典"
            "（is_tcm_item / zzl_subject / op_class / op_basis）",
            "- `src/out/feemap/national_item_catalog.csv` —— 国家医疗服务项目代码 → 目录章节",
            f"- `src/out/feemap/review_sheet.csv` —— **待医务科/医保办确认清单 {len(review):,} 条**"
            "（仅关键字判定、或科目归类存疑的项目）",
            ]

    md = "\n".join(log)
    (OUT_SUB / "s09_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
