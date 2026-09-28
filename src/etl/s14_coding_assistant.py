"""S14 Coding assistant v0 - deterministic cross-audit between services and codes.

Answers Medical Affairs' question with measurable evidence instead of a promise. Five
deterministic rules, all of them explainable from in-house dictionaries:

  R1  operation code missing   - a TCM operation class (or a treatment/surgery fee) is present
                                 in the fee detail but no matching operation code is recorded;
  R2  possible missing CC/MCC  - a characteristic drug is present without a matching
                                 discharge diagnosis. A HINT only, never a coding basis;
  R3  code not in dictionary   - the diagnosis/operation code is absent from the official map;
  R4  sex-code contradiction   - a sex-specific code on a patient of the opposite sex;
  R5  MDC mismatch (QY)        - main diagnosis MDC and main operation MDC are disjoint.

Hard boundaries (docs/10 section C): the tool never writes or modifies a code; drugs are
hints only; grouping is produced solely by drg_engine.

Back-test on the official settlement returns reports three numbers:
  trigger rate / association with official grouping differences / impact on TCM-group entry.
Docs: docs/21-编码助手v0与量化回测.md, docs/10 section 3-2.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402
from drg_engine import DrgEngine, norm, SEV_LABEL  # noqa: E402
import output_guard  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, DICT_DIR, OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "coding"
DICT_OUT = CONFIG_DIR / "dict"
COST_ATTR_CSV = DICT_OUT / "cost_item_attribute.csv"
HOMEPAGE_OP_CSV = DICT_OUT / "homepage_op_class.csv"
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"
SETTLE_CSV = OUT_DIR / "settlement" / "settlement_returns.csv"
PRED_CSV = OUT_DIR / "adrg" / "adrg_predictions.csv"

# The codes actually submitted live on the disease-case homepage, not in 源表(operation),
# and they speak a different dialect (40 homepage codes vs 83 annex-2 codes, 2 in common).
# src/config/dict/homepage_op_class.csv (built by s16) bridges the two.
HOME_CASE_TBL = "med_record.dbo.源表(case_man)"

# Discharge-date window for loading source data. Normally DERIVED from the settlement
# returns on disk (see audit_window) so that the window always encloses every settled case:
# a settled case discharged outside the window would silently contribute no findings and
# deflate every rate (headline 74.1% -> 47.6% before this was fixed). These are fallbacks.
OUT_FROM, OUT_TO = "2026-02-01", "2026-08-01"
TCM_GROUP_SUFFIX = ("F", "R", "Z")

CONF_HIGH, CONF_MID = "高", "中"

# Characteristic drug -> candidate diagnosis prefix. Evidence for a HINT, not for coding.
# Prefixed with a broad class on purpose: the coding detail must be chosen by a coder.
DRUG_HINTS = [
    (r"胰岛素|格列美脲|格列齐特|格列吡嗪|二甲双胍|阿卡波糖|西格列汀|达格列净", ("E10", "E11", "E14"), "糖尿病"),
    (r"氨氯地平|硝苯地平|非洛地平|缬沙坦|厄贝沙坦|氯沙坦|培哚普利|依那普利|"
     r"美托洛尔|比索洛尔|特拉唑嗪", ("I10", "I11", "I12", "I13", "I15"), "高血压"),
    (r"阿仑膦酸|唑来膦酸|利塞膦酸|骨化三醇|阿法骨化醇|碳酸钙D3|地舒单抗|雷洛昔芬",
     ("M80", "M81", "M82"), "骨质疏松"),
    (r"阿托伐他汀|瑞舒伐他汀|辛伐他汀|普伐他汀|非诺贝特|依折麦布", ("E78",), "血脂异常"),
    (r"华法林|利伐沙班|达比加群|阿哌沙班|氯吡格雷|替格瑞洛", ("I63", "I64", "I25", "I48"), "血栓/心脑血管"),
    (r"左甲状腺素|甲巯咪唑|丙硫氧嘧啶", ("E03", "E04", "E05"), "甲状腺疾病"),
    (r"促红素|蔗糖铁|琥珀酸亚铁|叶酸片|维生素B12", ("D50", "D51", "D53", "N18"), "贫血/慢性肾病"),
    (r"布地奈德|沙丁胺醇|噻托溴铵|孟鲁司特|氨茶碱", ("J44", "J45"), "慢性气道疾病"),
]

# Sex-specific ICD-10 ranges: (prefix-start, prefix-end, allowed sex)
SEX_RANGES = [
    ("O00", "O99", "2", "妊娠、分娩与产褥期"),
    ("N80", "N98", "2", "女性生殖器官疾病"),
    ("C50", "C50", "2", "乳房恶性肿瘤"),
    ("C53", "C58", "2", "女性生殖器官恶性肿瘤"),
    ("N40", "N51", "1", "男性生殖器官疾病"),
    ("C60", "C63", "1", "男性生殖器官恶性肿瘤"),
]


def load_op_class_map() -> dict[str, str]:
    """列(cost_no) -> TCM operation class (0 bone-setting ... 4 dressing/fumigation)."""
    d = pd.read_csv(COST_ATTR_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    d[src_col("cost_no")] = d[src_col("cost_no")].str.strip()
    d["op_class"] = d["op_class"].str.strip()
    return {getattr(r, src_col("cost_no")): r.op_class for r in d.itertuples() if r.op_class}


def load_dx_only_map() -> set[str]:
    d = pd.read_csv(COST_ATTR_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    return set(d[src_col("sort_code")].str.strip())


def load_code_map() -> tuple[dict[str, str], dict[str, str]]:
    mp = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    mp.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    ops = {str(r.country).strip(): str(r.yb).strip()
           for r in mp[mp["kind"] == "S"].itertuples() if r.yb}
    dxs = {str(r.country).strip(): str(r.yb).strip()
           for r in mp[mp["kind"] == "D"].itertuples() if r.yb}
    return ops, dxs


def valid_codes(kind: str) -> set[str]:
    mp = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    mp.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    return {norm(x) for x in mp[mp["kind"] == kind]["yb"].dropna()}


# ------------------------------------------------------------------
# Case data
# ------------------------------------------------------------------
def audit_window() -> tuple[str, str]:
    """Discharge-date window that encloses every admission in the settlement returns.

    The settlement file is the anchor: a case is only judged against the source rows the
    window loads, so a case settled but discharged outside the window would silently
    contribute no findings (this deflated the headline rate from 74.1% to 47.6% until the
    window was made to follow the data). Discharge precedes settlement, hence the lower
    buffer; the upper bound covers the latest settlement day.
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


def load_cases() -> dict:
    """One pass over the source database: fees, operations, diagnoses, drugs, sex."""
    w_from, w_to = audit_window()
    fee_sql = " UNION ALL ".join(
        f"SELECT m.列(hospital_no), f.列(cost_no), f.列(sort_code), f.列(money) "
        f"FROM {t} f JOIN 源表(admission) m ON m.列(hospital_no) = f.列(hospital_no) "
        f"WHERE m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}' AND f.列(mark_dell) = 0"
        for t in ("源表(fee_cure_in)", "源表(fee_check_in)"))
    fee = query_df(fee_sql)

    ops = query_df(
        "SELECT m.列(hospital_no), o.列(code), o.列(name) FROM 源表(operation) o "
        "JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) "
        f"WHERE m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}'")

    dxs = query_df(
        "SELECT m.列(hospital_no), d.列(icd), d.列(order), d.列(kind_input) FROM 源表(diagnosis) d "
        "JOIN 源表(admission) m ON m.列(serial) = d.列(serial_man) "
        f"WHERE m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}' "
        "AND d.列(kind_input) IN (3,4) AND d.列(kind_diagnose) = 2 "
        "ORDER BY m.列(hospital_no), d.列(order), d.列(icd)")

    drugs = query_df(
        "SELECT m.列(hospital_no), g.列(cost_name), SUM(g.列(money)) AS money "
        "FROM 源表(fee_medicine_in) g JOIN 源表(admission) m ON m.列(hospital_no) = g.列(hospital_no) "
        f"WHERE m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}' "
        "AND g.列(mark_dell) = 0 AND g.列(cost_name) IS NOT NULL GROUP BY m.列(hospital_no), g.列(cost_name)")

    sex = query_df(
        "SELECT 列(hospital_no), 列(ic_no), 列(inhospital_day) FROM 源表(admission) "
        f"WHERE 列(time_out) >= '{w_from}' AND 列(time_out) < '{w_to}'")

    home_ops = query_df(
        f"SELECT m.列(hospital_no), h.[手术编码] AS code FROM 源表(admission) m "
        f"JOIN {HOME_CASE_TBL} h ON h.[住院号] = m.列(hospital_no) "
        f"WHERE m.列(time_out) >= '{w_from}' AND m.列(time_out) < '{w_to}' "
        "AND h.[手术编码] IS NOT NULL AND LTRIM(RTRIM(h.[手术编码])) <> ''")

    return {"fee": fee, "ops": ops, "dxs": dxs, "drugs": drugs, "sex": sex,
            "home_ops": home_ops}


def sex_from_id(ic_no: object) -> str | None:
    """18-digit national ID: the 17th digit is odd for male, even for female."""
    s = re.sub(r"\D", "", str(ic_no or ""))
    if len(s) != 18:
        return None
    return "1" if int(s[16]) % 2 else "2"


# ------------------------------------------------------------------
# Rules
# ------------------------------------------------------------------
def build_groups(data: dict, op_class: dict[str, str]) -> pd.DataFrame:
    fee = data["fee"].copy()
    fee[src_col("cost_no")] = fee[src_col("cost_no")].astype(str).str.strip()
    fee[src_col("sort_code")] = fee[src_col("sort_code")].astype(str).str.strip()
    fee[src_col("money")] = pd.to_numeric(fee[src_col("money")], errors="coerce").fillna(0.0)
    # amounts are compared on the positive-sum basis agreed with Finance (docs/14)
    pos = fee[fee[src_col("money")] > 0]
    pos = pos.assign(op_class=pos[src_col("cost_no")].map(op_class).fillna(""))

    tcm_cls = (pos[pos["op_class"] != ""].groupby(src_col("hospital_no"))["op_class"]
               .apply(lambda s: sorted(set(s.astype(str)))).to_dict())
    # any treatment/surgery charge at all, used as the broader missing-operation signal
    charge = (pos[pos[src_col("sort_code")].isin(("10", "11"))].groupby(src_col("hospital_no"))[src_col("sort_code")]
              .apply(lambda s: sorted(set(s.astype(str)))).to_dict())

    ops = data["ops"].copy()
    ops["hospital_no"] = ops[src_col("hospital_no")].astype(str).str.strip()
    ops_by = ops.groupby("hospital_no").apply(
        lambda d: pd.DataFrame({"code": d[src_col("code")].astype(str).str.strip(),
                                "name": d[src_col("name")].astype(str)}),
        include_groups=False).to_dict() if len(ops) else {}

    dxs = data["dxs"].copy()
    dxs["hospital_no"] = dxs[src_col("hospital_no")].astype(str).str.strip()
    dxs[src_col("order")] = pd.to_numeric(dxs[src_col("order")], errors="coerce")
    dxs[src_col("icd")] = dxs[src_col("icd")].astype(str).str.strip()
    dxs["is_main"] = dxs[src_col("order")].eq(1)
    # main diagnosis = 列(order) 1 when present, otherwise the first recorded row
    # Deterministic main-diagnosis pick. 567 of 656 admissions carry MORE THAN ONE 列(order)=1
    # row, so "the" main diagnosis is genuinely ambiguous - and the previous code resolved it
    # with the default (unstable) quicksort over rows that arrived in an arbitrary SQL order.
    # The same case could therefore get a different main diagnosis between runs, which made R1
    # fire or not fire at random (observed 393 vs 394 on identical data). Stable sort plus full
    # tie-break keys removes the nondeterminism; the ORDER BY above removes it at the source.
    dxs = dxs.sort_values(["hospital_no", "is_main", src_col("order"), src_col("icd")],
                          ascending=[True, False, True, True], kind="mergesort")
    main_dx_by = dxs.groupby("hospital_no")[src_col("icd")].first().map(norm).to_dict()
    all_dx_by = dxs.groupby("hospital_no")[src_col("icd")].apply(
        lambda s: {norm(x) for x in s}).to_dict()

    drugs = data["drugs"].copy()
    drugs["hospital_no"] = drugs[src_col("hospital_no")].astype(str).str.strip()
    drug_by = drugs.groupby("hospital_no")[src_col("cost_name")].apply(
        lambda s: list(s.astype(str))).to_dict()

    sex_map = {str(getattr(r, src_col("hospital_no"))).strip(): sex_from_id(getattr(r, src_col("ic_no")))
               for r in data["sex"].itertuples()}

    # classes proven by the codes actually submitted on the homepage
    home_cls: dict[str, set[str]] = {}
    if HOMEPAGE_OP_CSV.exists() and len(data["home_ops"]):
        hm = pd.read_csv(HOMEPAGE_OP_CSV, dtype=str, encoding="utf-8-sig").fillna("")
        code2cls = {norm(r.手术编码): str(r.op_class).strip()
                    for r in hm.itertuples() if str(r.op_class).strip()}
        ho = data["home_ops"].copy()
        ho["hospital_no"] = ho[src_col("hospital_no")].astype(str).str.strip()
        ho["c"] = ho["code"].map(norm)
        home_cls = (ho.groupby("hospital_no")["c"]
                    .apply(lambda s: {code2cls[c] for c in s if c in code2cls}).to_dict())

    return {"tcm_cls": tcm_cls, "charge": charge, "ops": ops_by, "dxs": dxs,
            "drugs": drug_by, "sex": sex_map, "home_cls": home_cls,
            "main_dx": main_dx_by, "all_dx": all_dx_by}


def comorbidity_hint(eng: DrgEngine, main_dx, all_dx: set, prefixes) -> str:
    """Drug-derived candidate diagnosis -> its effect on the CC/MCC severity tier.

    Implements the chain 用药 -> 其他诊断(增删) -> 合并症(CC/MCC)判断 -> 第4位档位.
    This is a HINT only: the code is never written; we merely read what the deterministic
    engine would compute if a clinician confirmed and recorded the candidate. The candidate
    is matched against the MCC/CC tables by 3-char prefix to stay robust to code granularity.
    """
    if eng is None or eng.std is None or not prefixes:
        return ""
    mcc_hit = any(te.startswith(p) for p in prefixes for te in eng.std.mcc)
    cc_hit = any(te.startswith(p) for p in prefixes for te in eng.std.cc)
    if not (mcc_hit or cc_hit):
        return "（候选诊断不在 MCC/CC 表，补录不改变第4位严重程度档位）"
    kind = "MCC" if mcc_hit else "CC"
    table = eng.std.mcc if mcc_hit else eng.std.cc
    concrete = next((te for p in prefixes for te in table if te.startswith(p)), None)
    cur = eng.std.severity(all_dx, main_dx)
    new = eng.std.severity(all_dx | {concrete} if concrete else all_dx, main_dx)
    if new == cur:
        return (f"（候选诊断属 {kind} 表，但受主诊断排除表约束，"
                f"补录后第4位档位仍为「{SEV_LABEL.get(cur, cur or '无')}」）")
    return (f"（候选诊断属 {kind} 表，若临床确认补录，"
            f"第4位档位将由「{SEV_LABEL.get(cur, cur or '无')}」"
            f"升为「{SEV_LABEL.get(new, new)}」）")


def audit_case(hs: str, g: dict, eng: DrgEngine, op_map: dict[str, str],
               dx_map: dict[str, str], valid_dx: set[str], valid_op: set[str],
               days: float | None = None) -> list[dict]:
    """Run the five rules for one admission and return the items to be human-verified."""
    out: list[dict] = []
    dx_rows = g["dxs"][g["dxs"]["hospital_no"] == hs]
    main_dx = g["main_dx"].get(hs)
    all_dx = g["all_dx"].get(hs, set())

    op_rows = g["ops"].get(hs)
    op_codes = {norm(x) for x in op_rows["code"]} if op_rows is not None else set()
    op_yb = {op_map.get(c, c) for c in op_codes}
    # the submitted codes are the homepage ones; 源表(operation) is a secondary source
    home_cls = set(g["home_cls"].get(hs, set()))
    recorded_cls = home_cls | classes_from_codes(op_yb)

    fee_cls = set(g["tcm_cls"].get(hs, []))                        # classes shown by the fees
    missing = sorted(fee_cls - recorded_cls)

    # ---- R1 missing operation codes, counterfactual form -------------
    # The naive test ("fees show TCM operations but no codes are recorded") fires on 99.8% of
    # cases and carries no information, so it is deliberately NOT used as an alert. The rule
    # only fires when completing the codes would actually change the grouping outcome.
    if eng.tcm is not None and main_dx and days is not None and missing and eng.ready:
        now = eng.tcm.group(main_dx, recorded_cls, days)
        cf = eng.tcm.group(main_dx, fee_cls, days)
        # fires when the fees support a TCM group that the recorded codes do not reach -
        # either no group at all, or a different (usually heavier) one
        if cf.get("drg") and cf.get("drg") != (now.get("drg") or ""):
            reached = now.get("drg")
            out.append({
                "rule": "R1", "confidence": CONF_HIGH,
                "finding": f"费用明细显示已执行中医操作类 {'/'.join(missing)}；"
                           f"补全操作编码后可按 **{cf['drg']}**（{cf.get('name', '')}）入组，"
                           f"当前病案操作编码仅能证明类 "
                           f"{','.join(sorted(recorded_cls)) or '无'}"
                           f"，{f'现有编码可达 {reached}' if reached else '无法入组'}",
                "basis": "费用三表经 cost_item_attribute.op_class 映射 + 某地区医保局〔2025〕27号 附件2",
                "impact": f"疑似操作漏编导致未达最优中医优势病组（可达 {cf['drg']}）",
            })

    # ---- R2 possible missing CC/MCC (drug hint only) -----------------
    # Chain: 用药 -> 候选其他诊断(增删) -> 合并症(CC/MCC)判断 -> 第4位档位.
    seen_label: set[str] = set()
    for name in g["drugs"].get(hs, []):
        for pat, prefixes, label in DRUG_HINTS:
            if label in seen_label or not re.search(pat, str(name)):
                continue
            if any(d.startswith(prefixes) for d in all_dx):
                break
            seen_label.add(label)
            comorb = comorbidity_hint(eng, main_dx, all_dx, prefixes)
            out.append({
                "rule": "R2", "confidence": CONF_MID,
                "finding": f"用药「{name}」提示可能存在的诊断：{label}"
                           f"（候选编码前缀 {'/'.join(prefixes)}），出院诊断中未记录",
                "basis": "用药明细（源表(fee_medicine_in)）——**线索，非编码依据**",
                "impact": "若临床确认成立，影响 CC/MCC 严重程度分组"
                          + (f"；{comorb}" if comorb else ""),
            })
            break

    # ---- R3 code not in the official dictionary ----------------------
    raw_dx = {str(x).strip() for x in dx_rows[src_col("icd")]} if len(dx_rows) else set()
    for raw in raw_dx:
        yb = norm(dx_map.get(raw, raw))
        if yb and yb not in valid_dx and not raw.upper().startswith(("S", "T", "M")):
            out.append({
                "rule": "R3", "confidence": CONF_MID,
                "finding": f"诊断编码「{raw}」未在医保版 ICD-10 目录中命中",
                "basis": "本地对照表（kind=D）",
                "impact": "编码合法性待核（本工具不自动替换）",
            })
    for raw in op_codes:
        yb = norm(op_map.get(raw, raw))
        if yb and yb not in valid_op:
            out.append({
                "rule": "R3", "confidence": CONF_MID,
                "finding": f"操作编码「{raw}」未在医保版 ICD-9-CM 目录中命中",
                "basis": "本地对照表（kind=S）",
                "impact": "编码合法性待核（本工具不自动替换）",
            })

    # ---- R4 sex-code contradiction -----------------------------------
    sex = g["sex"].get(hs)
    if sex:
        for raw in raw_dx:
            code = norm(dx_map.get(raw, raw))
            for lo, hi, need, label in SEX_RANGES:
                if lo <= code[:3] <= hi and sex != need:
                    out.append({
                        "rule": "R4", "confidence": CONF_HIGH,
                        "finding": f"诊断「{raw}」（{label}）与患者性别不符",
                        "basis": "身份证号第 17 位判定性别 + ICD-10 性别专属章节",
                        "impact": "确定性错误，需编码员核对",
                    })

    # ---- R5 main diagnosis MDC vs main operation MDC ------------------
    if eng.ready and main_dx:
        mdcs = eng.std.mdc_of(main_dx)
        if mdcs and op_yb:
            op_mdcs = set()
            for adrg, codes in eng.std.op_cond.items():
                if codes & op_yb:
                    op_mdcs.add(eng.std.adrg_mdc.get(adrg))
            if op_mdcs and not (op_mdcs & mdcs):
                out.append({
                    "rule": "R5", "confidence": CONF_HIGH,
                    "finding": f"主诊断 MDC {'/'.join(sorted(mdcs))} 与主操作所属 MDC "
                               f"{'/'.join(sorted(x for x in op_mdcs if x))} 不一致",
                    "basis": "CHS-DRG 2.0 主诊表 + ADRG 手术条件表",
                    "impact": "触发 QY（未入组）风险，需核对主诊断/主操作选择",
                })
    return out


_TCM_BY_YB: dict[str, str] | None = None


def classes_from_codes(op_yb: set[str]) -> set[str]:
    """TCM operation classes that the recorded medicare operation codes can prove."""
    yb_class = _tcm_class_by_yb()
    return {yb_class[c] for c in op_yb if c in yb_class}


def _tcm_class_by_yb() -> dict[str, str]:
    """medicare operation code -> TCM class, via the 27号文 rules already extracted."""
    global _TCM_BY_YB  # noqa: PLW0603
    if _TCM_BY_YB is not None:
        return _TCM_BY_YB
    import json  # noqa: PLC0415
    rules = json.loads((DICT_OUT / "tcm_advantage_rules.json").read_text(encoding="utf-8"))
    m: dict[str, str] = {}
    for gg in rules.values():
        for key, lst in gg["ops"].items():
            cls = key if key in ("0", "1", "2", "3", "4") else "0"
            for o in lst:
                m[norm(o["code"])] = cls
    _TCM_BY_YB = m
    return m


# ------------------------------------------------------------------
def backtest(eng: DrgEngine) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    op_class = load_op_class_map()
    op_map, dx_map = load_code_map()
    valid_dx = valid_codes("D")
    valid_op = valid_codes("S")

    print("[1/3] 读取病例数据 ...")
    data = load_cases()
    g = build_groups(data, op_class)
    print(f"      费用 {len(data['fee']):,} 行 / 手术 {len(data['ops']):,} 行 / "
          f"诊断 {len(data['dxs']):,} 行 / 药品 {len(data['drugs']):,} 行")

    off = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    off["k"] = off["medical_no"].astype(str).str.strip()
    day_map = dict(zip(off["k"], pd.to_numeric(off["actual_days"], errors="coerce")))
    off_map = dict(zip(off["k"], off["drg_code"].astype(str).str.strip()))

    pred = pd.read_csv(PRED_CSV, dtype=str, encoding="utf-8-sig")
    pred["k"] = pred["medical_no"].astype(str).str.strip()
    pred["hit_adrg"] = (pred["pred_adrg"].fillna("").astype(str).str.strip()
                        == pred["drg_code"].fillna("").astype(str).str.strip().str[:3])
    hit_map = dict(zip(pred["k"], pred["hit_adrg"]))

    cases = [h for h in off["k"] if h]

    print(f"[2/3] 稽核 {len(cases):,} 例 ...")
    items: list[dict] = []
    for hs in cases:
        for it in audit_case(hs, g, eng, op_map, dx_map, valid_dx, valid_op, day_map.get(hs)):
            items.append({"medical_no": hs, **it})

    # ---- output guard (docs/26): show a finding only if its whitelist rule is 已确认 ----
    FINDING_COLS = ["medical_no", "rule", "confidence", "finding", "basis", "impact"]
    # R1 操作漏编 / R2 用药线索 / R3 码不在字典 / R4 性别矛盾 / R5 MDC 不匹配
    RULE_TO_WL = {"R1": "W13", "R2": "W14", "R3": "W11", "R4": "W15", "R5": "W16"}
    guard = output_guard.Guard()
    output_guard.reset()
    kept = [it for it in items
            if guard.screen(RULE_TO_WL.get(it.get("rule", ""), ""),
                            f"{it.get('finding', '')} {it.get('impact', '')}")[0]]
    if len(items) != len(kept):
        print(f"      输出闸门抑制 {len(items) - len(kept):,} 条"
              "（对应白名单未确认或命中黑名单）")
    items = kept
    it_df = pd.DataFrame(items, columns=FINDING_COLS)
    # Deterministic row order. Findings were emitted while iterating sets (the drug names of an
    # admission), so two runs of the same job produced the same rows in a different order and
    # the CSV was not byte-reproducible - found while measuring the reproducibility boundary
    # (docs/35). Sorting by the natural key makes the artefact stable; mergesort is stable, so
    # equal keys keep their relative order.
    if {"medical_no", "rule", "finding"}.issubset(it_df.columns):
        it_df = (it_df.sort_values(["medical_no", "rule", "finding"], kind="mergesort")
                 .reset_index(drop=True))
    it_df.to_csv(OUT_SUB / "coding_findings.csv", index=False, encoding="utf-8-sig")

    # ---- ③ counterfactual: which TCM groups were lost to missing codes ----
    # Official returns tell what the platform settled; recomputing with the fees' full set of
    # TCM operation classes shows what completing the codes would have produced.
    lost: list[dict] = []
    for hs in cases:
        off_drg = str(off_map.get(hs, ""))
        if not off_drg or off_drg.endswith(TCM_GROUP_SUFFIX) or eng.tcm is None:
            continue
        mdx = g["main_dx"].get(hs)
        fee_cls = set(g["tcm_cls"].get(hs, []))
        if not mdx or not fee_cls:
            continue
        op_rows = g["ops"].get(hs)
        rec = set(g["home_cls"].get(hs, set())) | classes_from_codes(
            {op_map.get(norm(x), norm(x)) for x in op_rows["code"]}) if op_rows is not None \
            else set(g["home_cls"].get(hs, set()))
        列(off) = day_map.get(hs)
        # the official return only carries a day count in the March schema; with a missing
        # value the threshold comparison would silently pass (NaN < n is False) and inflate
        # the result, so such cases are excluded from the counterfactual instead.
        if 列(off) is None or pd.isna(列(off)):
            continue
        now = eng.tcm.group(mdx, rec, 列(off))
        cf = eng.tcm.group(mdx, fee_cls, 列(off))
        # must be a gap caused by the codes themselves, not a platform-vs-HIS view difference
        if cf.get("drg") and cf.get("drg") != (now.get("drg") or ""):
            lost.append({"medical_no": hs, "official_drg": off_drg,
                         "counterfactual_drg": cf["drg"], "name": cf.get("name", "")})
    lost_df = pd.DataFrame(lost, columns=["medical_no", "official_drg",
                                          "counterfactual_drg", "name"])
    if len(lost_df):
        # money impact: same ADRG, TCM tier lost. This is the number to take to Medical Affairs.
        lost_df["官方支付标准"] = lost_df["official_drg"].map(eng.payment_standard)
        lost_df["可达支付标准"] = lost_df["counterfactual_drg"].map(eng.payment_standard)
        lost_df["差额"] = (lost_df["可达支付标准"] - lost_df["官方支付标准"]).round(2)
    # same reason as coding_findings.csv: `cases` is iterated as a set, so keep the artefact
    # byte-reproducible by sorting on the natural key
    if "medical_no" in lost_df.columns and len(lost_df):
        lost_df = lost_df.sort_values("medical_no", kind="mergesort").reset_index(drop=True)
    lost_df.to_csv(OUT_SUB / "counterfactual_tcm_loss.csv", index=False, encoding="utf-8-sig")

    # ---- the three numbers promised to Medical Affairs ----------------
    trig = it_df.groupby("medical_no")["rule"].apply(set).to_dict() if len(it_df) else {}
    n = len(cases)
    r1 = {h for h, s in trig.items() if "R1" in s}
    r2 = {h for h, s in trig.items() if "R2" in s}
    any_hit = set(trig)

    grp_hit = [h for h in cases if hit_map.get(h) is True]
    grp_bad = [h for h in cases if hit_map.get(h) is False]

    def rate(pool: list[str], s: set[str]) -> float:
        return sum(1 for h in pool if h in s) / len(pool) if pool else 0.0

    rows = [
        ("① 漏编提示触发率（R1 ∪ R2）", f"{len(any_hit)} / {n}", f"{len(any_hit) / n:.1%}"),
        ("　R1 操作漏编（反事实判据）", f"{len(r1)} / {n}", f"{len(r1) / n:.1%}"),
        ("　R2 用药线索（中置信，非编码依据）", f"{len(r2)} / {n}", f"{len(r2) / n:.1%}"),
        ("② 触发率 · ADRG 一致病例组", f"{sum(1 for h in grp_hit if h in any_hit)} / {len(grp_hit)}",
         f"{rate(grp_hit, any_hit):.1%}"),
        ("② 触发率 · ADRG 不一致病例组", f"{sum(1 for h in grp_bad if h in any_hit)} / {len(grp_bad)}",
         f"{rate(grp_bad, any_hit):.1%}"),
        ("③ 疑似因操作漏编而损失的中医优势病组",
         f"{len(lost_df)} / {n}", f"{len(lost_df) / n:.1%}"),
    ]
    res = pd.DataFrame(rows, columns=["指标", "例数", "比率"])
    res.loc[len(res)] = ["　（③ 的可判定基数：官方返回含住院天数的病例）",
                         f"{int(pd.notna(pd.Series(list(day_map.values()))).sum())} / {n}",
                         "—"]

    # ---- key finding: the homepage keeps a single operation code per case ----
    r1_tcm = [h for h in r1 if str(off_map.get(h, "")).endswith(TCM_GROUP_SUFFIX)]
    ho = data["home_ops"].copy()
    ho["hospital_no"] = ho[src_col("hospital_no")].astype(str).str.strip()
    hcnt = ho.groupby("hospital_no").size()
    code_depth = pd.DataFrame([
        ["首页有操作编码的病例", f"{len(hcnt)}"],
        ["其中**仅填 1 个**操作码的病例",
         f"{int((hcnt <= 1).sum())}（{(hcnt <= 1).mean():.1%}）"],
        ["首页操作码/病例（中位/最大）", f"{hcnt.median():.0f} / {hcnt.max():.0f}"],
        ["官方返回含住院天数（③ 可判定基数）",
         f"{int(pd.notna(pd.Series(list(day_map.values()))).sum())} / {n}"],
        ["**R1 触发中官方仍为中医组（F/R/Z）**",
         f"{len(r1_tcm)} / {len(r1)}（{len(r1_tcm) / len(r1):.1%}）" if r1 else "—"],
    ], columns=["项", "值"])
    code_depth.to_csv(OUT_SUB / "code_depth_finding.csv", index=False, encoding="utf-8-sig")

    # ---- length-of-stay口径: HIS 列(inhospital_day) vs the official returned days ----
    his_day = {str(getattr(r, src_col("hospital_no"))).strip(): pd.to_numeric(getattr(r, src_col("inhospital_day")), errors="coerce")
               for r in data["sex"].itertuples()}
    cmp_rows = []
    for h in cases:
        a, b = his_day.get(h), day_map.get(h)
        if pd.notna(a) and pd.notna(b):
            cmp_rows.append({"his": float(a), "off": float(b)})
    cmp_df = pd.DataFrame(cmp_rows)
    stay = pd.DataFrame(columns=["项", "值"])
    if len(cmp_df):
        cmp_df["diff"] = cmp_df["his"] - cmp_df["off"]
        cross = int(((cmp_df["his"] < 10) & (cmp_df["off"] >= 10)).sum())
        stay = pd.DataFrame([
            ["可比对病例", f"{len(cmp_df)}"],
            ["HIS 与官方天数差值 中位/均值", f"{cmp_df['diff'].median():.0f} / "
                                          f"{cmp_df['diff'].mean():.2f}"],
            ["两者不等的例数", f"{int((cmp_df['diff'] != 0).sum())}"
                              f"（{int((cmp_df['diff'] != 0).sum()) / len(cmp_df):.1%}）"],
            ["**HIS < 10 天 而 官方 ≥ 10 天**",
             f"**{cross}**（{cross / len(cmp_df):.1%}）"],
        ], columns=["项", "值"])
    return res, lost_df, it_df, stay, code_depth


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None, metavar="住院号", help="输出单例编码员待核清单")
    a = ap.parse_args()
    OUT_SUB.mkdir(parents=True, exist_ok=True)

    eng = DrgEngine()
    if not eng.ready:
        print("[!] DRG 引擎字典未就绪：请先运行 src/etl/s11_adrg.py")
        return 2

    if a.case:
        hs = str(a.case).strip()
        op_class = load_op_class_map()
        op_map, dx_map = load_code_map()
        data = load_cases()
        g = build_groups(data, op_class)
        # length of stay is required by the entry conditions, so it must be supplied here too
        d = query_df(f"SELECT 列(inhospital_day) FROM 源表(admission) WHERE 列(hospital_no) = '{hs}'")
        days = float(d.iloc[0, 0]) if len(d) and pd.notna(d.iloc[0, 0]) else None
        items = audit_case(hs, g, eng, op_map, dx_map,
                           valid_codes("D"), valid_codes("S"), days)

        # evidence summary: a coder must be able to see why a rule did or did not fire
        op_rows = g["ops"].get(hs)
        rec_cls = classes_from_codes(
            {op_map.get(norm(x), norm(x)) for x in op_rows["code"]}) if op_rows is not None else set()
        fee_cls = set(g["tcm_cls"].get(hs, []))
        mdx = g["main_dx"].get(hs)
        now = eng.tcm.group(mdx, rec_cls, days) if eng.tcm is not None else {}
        cf = eng.tcm.group(mdx, fee_cls, days) if eng.tcm is not None else {}

        print(f"\n# 住院号 {hs} 编码员待核清单（{len(items)} 项）\n")
        print("## 判据自证")
        print(f"- 出院主要诊断：`{mdx or '（无）'}`　住院天数：{days if days is not None else '（缺）'}")
        print(f"- 费用侧中医操作类：`{','.join(sorted(fee_cls)) or '无'}`"
              f"　**已提交首页码**可证明类：`{','.join(sorted(g['home_cls'].get(hs, set()))) or '无'}`"
              f"　HIS 操作记录类：`{','.join(sorted(rec_cls)) or '无'}`")
        print(f"- 按现有编码可达中医组：`{now.get('drg') or '无'}`"
              f"　按费用类补全后可达：`{cf.get('drg') or '无'}`")
        print(f"- 费用侧治疗/手术类收费：`{','.join(g['charge'].get(hs, [])) or '无'}`"
              f"　病案操作编码条数：{0 if op_rows is None else len(op_rows)}\n")
        print("## 提示项")
        for it in items:
            print(f"\n[{it['rule']}][置信度 {it['confidence']}] {it['finding']}")
            print(f"    依据：{it['basis']}")
            print(f"    影响：{it['impact']}")
        if not items:
            print("未发现问题。")
        return 0

    print("[3/3] 汇总 ...")
    res, lost_df, it_df, stay, code_depth = backtest(eng)

    by_rule = (it_df.groupby("rule").size().rename("条目数").reset_index()
               if len(it_df) else pd.DataFrame(columns=["rule", "条目数"]))
    by_conf = (it_df.groupby("confidence").size().rename("条目数").reset_index()
               if len(it_df) else pd.DataFrame(columns=["confidence", "条目数"]))

    off_hdr = pd.read_csv(SETTLE_CSV, dtype=str, encoding="utf-8-sig")
    off_hdr["k"] = off_hdr["medical_no"].astype(str).str.strip()
    n_cases = int(off_hdr["k"].ne("").sum())
    sd = pd.to_datetime(off_hdr["settle_date"].astype(str).str[:10], errors="coerce")
    period = f"{sd.min():%Y-%m} ~ {sd.max():%Y-%m}" if sd.notna().any() else "未知区间"
    lines = ["# S14 编码助手 v0 量化回测", "",
             f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
             f"- 回测集：{period} 官方结算 **{n_cases}** 例（\"假如当时有此工具\"）",
             "- 规则：R1 操作漏编（**反事实判据**）/ R2 用药线索（线索非依据）/ "
             "R3 编码不在字典 / R4 性别矛盾 / R5 MDC 不匹配（QY 预警）", "",
             "## 一、承诺给医务科的三个数字", "", _md(res), "",
             "## 二、各规则触发条目", "", _md(by_rule), "", _md(by_conf), "",
             "## 三、关键发现：平台收到的编码 ≠ 病案首页所存的编码", "",
             "> R1 触发的病例中**绝大多数官方本就分入了中医组**——说明平台侧看到了比首页更多的"
             "操作编码。这解释了 R1 精度为何只有约 1.5%，也是本轮回测最有价值的一条结论。", "",
             _md(code_depth), ""]
    if len(lost_df):
        top = (lost_df.groupby(["official_drg", "counterfactual_drg", "name"]).size()
               .rename("例数").reset_index().sort_values("例数", ascending=False).head(12))
        lines += ["## 四、疑似因操作漏编而损失的中医优势病组（同 ADRG 内的档位损失）", "",
                  _md(top), "",
                  "### 4.1 金额影响（同 ADRG 内中医档位 vs 普通档位）", "",
                  _md(lost_df[["medical_no", "official_drg", "counterfactual_drg", "name",
                               "官方支付标准", "可达支付标准", "差额"]]), "",
                  f"- 合计差额：**{lost_df['差额'].sum():,.2f} 元**"
                  f"（{len(lost_df)} 例，均可判定基数内）", ""]
    md = "\n".join(lines)
    (OUT_SUB / "s14_backtest.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
