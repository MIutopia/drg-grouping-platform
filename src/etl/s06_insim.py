"""S06 In-hospital DRG simulator v1 (attending physician view).

Produces the four-line output per in-hospital patient and a per-physician daily list.
Grouping is delegated to drg_engine (TCM overlay first, then standard CHS-DRG), so both
paths report a real group code, weight and payment standard.
Compliance guardrails are enforced here, not in the report text.
Docs: docs/15-代码模块说明与作业手册.md §6.7, docs/19-引擎接入在院模拟器.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402
import ddlio  # noqa: E402
from drg_engine import COEF, FEE_RATE, PATH_NONE, PATH_STD, PATH_TCM, DrgEngine  # noqa: E402
import output_guard  # noqa: E402
from tcm_ratio import cmr_status, executed_op_classes, ratio, subject_totals  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, DICT_DIR, OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

# 27号文 operation classes: 0 bone-setting, 1 tuina, 2 needling, 3 moxibustion,
# 4 dressing/fumigation.
TREAT_CLASSES = ("1", "2", "3", "4")

OUT_SUB = OUT_DIR / "insim"
RULES_JSON = OUT_DIR / "tcm" / "tcm_advantage_rules.json"
CATALOG_CSV = CONFIG_DIR / "dict" / "drg_group_catalog.csv"
OP_MAP_CSV = DICT_DIR / "本地对照表.csv"

DISCLAIMER = "基于当前已录入信息推演，分组结果以医保平台为准"

BASIS_TCM = "中医优势病组规则"
BASIS_NONE = "未分组"


def norm(c: object) -> str:
    return str(c).strip().upper()


def load_op_map(kind: str = "S") -> dict[str, str]:
    """Map in-house codes to the medicare version ('S' operations, 'D' diagnoses)."""
    mp = pd.read_csv(OP_MAP_CSV, dtype=str, encoding="utf-8-sig")
    mp.columns = [src_col("time_input"), "country", "country_name", "yb", "yb_name", "kind", "yb_old"]
    return {str(r.country).strip(): str(r.yb).strip()
            for r in mp[mp["kind"] == kind].itertuples() if r.yb}


def load_rules() -> dict[str, dict]:
    g = json.loads(RULES_JSON.read_text(encoding="utf-8"))
    for gg in g.values():
        for d in gg["dx"]:
            d["code"] = norm(d["code"])
        for lst in gg["ops"].values():
            for o in lst:
                o["code"] = norm(o["code"])
    return g


# ------------------------------------------------------------------
# Inclusion evaluation and gap analysis
# ------------------------------------------------------------------
def evaluate(dx: str | None, classes: set[str], days: int, groups: dict) -> dict:
    """Judge inclusion from diagnosis, executed TCM operation classes and length of stay.

    Operation classes come from the advice table (see tcm_ratio.executed_op_classes),
    because the medical-record operation table is only filled at the coding stage.
    """
    dx_n = norm(dx) if dx else ""
    res: dict = {"matched": [], "near": []}

    for c, g in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        dx_set = {d["code"] for d in g["dx"]}
        day_min = g["days"] or 0
        dx_ok = bool(dx_n) and dx_n in dx_set
        day_ok = days >= day_min

        if g["op_mode"] == "any":
            # 中医正骨术 group: class 0 (bone-setting) satisfies it
            op_ok = "0" in classes
            covered_n = 1 if op_ok else 0
            need = 1
            lacking = "" if op_ok else "0"
            op_note = "已执行中医正骨类操作" if op_ok else "未执行中医正骨类操作（类 0）"
        else:
            allowed = {k for k in g["ops"] if k != "main"} & set(TREAT_CLASSES)
            covered = sorted(classes & allowed)
            need = g.get("min_ops", 99)
            op_ok = len(covered) >= need
            covered_n = len(covered)
            lacking = ",".join(sorted(allowed - set(covered)))
            op_note = f"已覆盖中医操作类 {','.join(covered) or '无'}（{covered_n}/{need} 类）"

        if dx_ok and day_ok and op_ok:
            res["matched"].append(c)
            continue

        # Only report gaps for groups whose diagnosis list already matches; otherwise the
        # output would be flooded with irrelevant groups and become clinical noise.
        if not dx_ok:
            continue

        miss = []
        if not day_ok:
            miss.append(f"住院天数不足（当前 {days} 天，要求 ≥{day_min} 天）")
        if not op_ok:
            miss.append(op_note)
        res["near"].append({
            "drg": c, "name": g["name"], "days_min": day_min,
            "miss": miss, "lacking_op_groups": lacking,
            "covered": covered_n, "need": need,
        })

    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doctor", default=None, help="经管医师工号（列(doctor_residency) 的前缀）")
    ap.add_argument("--date", default=None, help="统计日期 YYYY-MM-DD，默认取数据库当日")
    args = ap.parse_args()

    groups = load_rules()
    op_map = load_op_map("S")
    dx_map = load_op_map("D")
    eng = DrgEngine()
    if not eng.ready:
        print("[!] DRG 引擎字典未就绪：请先运行 src/etl/s11_adrg.py 生成 config/dict/drg_*.csv")
        return 2

    # Output guard: every line below is emitted only when its whitelist rule is 已确认 and it
    # misses the blacklist. Fails closed - an unreadable whitelist suppresses everything rather
    # than falling back to "show it all", so the 审议 outcome is a real control (docs/26).
    guard = output_guard.Guard()
    output_guard.reset()
    if not guard.available:
        print("[!] 白名单不可读：本次所有受控输出将被抑制（失败即关闭）")

    # ---- in-hospital patients --------------------------------------
    sql = ("SELECT 列(serial), 列(hospital_no), 列(doctor_residency), 列(office_now), "
           "列(time_enter), DATEDIFF(DAY, 列(time_enter), GETDATE()) + 1 AS days_now "
           "FROM 源表(admission) WHERE 列(mark_out) = 0 AND 列(time_out) IS NULL")
    pat = query_df(sql)
    if pat.empty:
        print("本单位当前无在院患者（列(mark_out)=0）")
        return 0

    # ---- diagnoses / operations / fees -----------------------------
    # all in-hospital western diagnoses: 列(order)=1 is the main one, the rest feed CC/MCC
    dxa = query_df(
        "SELECT d.列(serial_man), d.列(order), d.列(icd), d.列(note) FROM 源表(diagnosis) d "
        "JOIN 源表(admission) m ON m.列(serial) = d.列(serial_man) "
        "WHERE m.列(mark_out) = 0 AND d.列(kind_input) IN (1,2) AND d.列(kind_diagnose) = 2 "
        "ORDER BY d.列(serial_man), d.列(order), d.列(icd)")
    dxa["yb"] = dxa[src_col("icd")].astype(str).str.strip().map(dx_map).fillna(
        dxa[src_col("icd")].astype(str).str.strip())
    dxa["ord"] = pd.to_numeric(dxa[src_col("order")], errors="coerce")
    dx = dxa[dxa["ord"].eq(1)].drop_duplicates(src_col("serial_man")).set_index(src_col("serial_man"))
    other_map = (dxa[~dxa["ord"].eq(1)].groupby(src_col("serial_man"))["yb"]
                 .apply(lambda s: set(s.astype(str))).to_dict())

    # Primary source: executed orders. The medical-record operation table is only filled
    # at the coding stage, so it cannot drive an in-hospital view (see docs/16).
    adv = executed_op_classes("sbo", "m.列(mark_out) = 0")
    cls_map = dict(zip(adv[src_col("hospital_no")], adv["op_classes"])) if not adv.empty else {}
    adv_name = dict(zip(adv[src_col("hospital_no")], adv["op_names"])) if not adv.empty else {}

    # Secondary source: already coded operations on the medical record (if any)
    ops = query_df(
        "SELECT o.列(id_parent), o.列(order), o.列(code), o.列(name) FROM 源表(operation) o "
        "JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) WHERE m.列(mark_out) = 0")
    ops["eff"] = ops[src_col("code")].astype(str).str.strip().map(op_map).fillna(
        ops[src_col("code")].astype(str).str.strip())
    code_class: dict[str, str] = {}
    for g in groups.values():
        for k, lst in g["ops"].items():
            cls = k if k in TREAT_CLASSES else "0"
            for o in lst:
                code_class[norm(o["code"])] = cls
    ops["cls"] = ops["eff"].map(lambda c: code_class.get(norm(c), ""))
    ops_by_case = ops[ops["cls"] != ""].groupby(src_col("id_parent"))["cls"].apply(
        lambda s: set(s.astype(str))).to_dict()
    op列(name) = ops.groupby(src_col("id_parent"))[src_col("name")].apply(lambda s: "、".join(s.astype(str))).to_dict()
    # medicare-version operation codes per case, required by the standard ADRG path
    op_codes_by_case = ops.groupby(src_col("id_parent"))["eff"].apply(
        lambda s: {str(x) for x in s}).to_dict()

    # Fees, split into the 中治率 subjects
    totals = subject_totals("sbo", "m.列(mark_out) = 0")
    tot = ratio(totals).reset_index()
    subj_cols = [c for c in tot.columns
                 if c not in (src_col("hospital_no"), "zzl_numerator", "zzl_denominator", "zzl_ratio")]
    tot["fee_all"] = tot[subj_cols].sum(axis=1).round(2)
    fee_map = dict(zip(tot[src_col("hospital_no")], tot["fee_all"]))
    # 中治率 verdict from the RAW ratio (cmr_status), never a pre-rounded one (see tcm_ratio)
    cs = cmr_status(totals)
    zzl_raw_map = dict(zip(cs.index, cs["zzl_raw"]))
    zzl_pass_map = dict(zip(cs.index, cs["zzl_passed"]))

    # ---- physician phone mapping (docs/29: 手机号代替工号) -----------
    doc = ddlio.query("SELECT doctor_code, doctor_name, doctor_phone FROM sys_doctor", "drg")
    phone_map = dict(zip(doc["doctor_code"].astype(str).str.strip(),
                         doc["doctor_phone"].astype(str).str.strip()))
    # 姓名兜底：通讯录缺工号者以手机号代工号，若不按姓名兜底，这些人将来成为责任医师时匹配不上
    name_map = dict(zip(doc["doctor_name"].astype(str).str.strip(),
                        doc["doctor_phone"].astype(str).str.strip()))
    unmapped: set[str] = set()

    # ---- per-case projection ---------------------------------------
    rows = []
    for r in pat.itertuples():
        doc_raw = getattr(r, src_col("doctor_residency"))
        doc_code = doc_name = None
        if doc_raw is not None and not (isinstance(doc_raw, float) and pd.isna(doc_raw)):
            parts = str(doc_raw).split("|", 1)
            doc_code = parts[0].strip()
            doc_name = parts[1].strip() if len(parts) > 1 else None
        # 工号优先；工号查不到时按姓名兜底（通讯录缺工号者以手机号代工号）
        doc_phone = phone_map.get(doc_code) if doc_code else None
        if not doc_phone and doc_name:
            doc_phone = name_map.get(doc_name)
        if doc_code and doc_code not in phone_map and (not doc_name or doc_name not in name_map):
            unmapped.add(doc_code)
        rec = dx.loc[getattr(r, src_col("serial"))] if getattr(r, src_col("serial")) in dx.index else None
        dx_code = str(rec[src_col("icd")]).strip() if rec is not None else None
        dx_yb = str(rec["yb"]).strip() if rec is not None else dx_code
        cset = set(cls_map.get(getattr(r, src_col("hospital_no")), []) or []) | ops_by_case.get(getattr(r, src_col("serial")), set())
        oset = op_codes_by_case.get(getattr(r, src_col("serial")), set())
        dx_other = other_map.get(getattr(r, src_col("serial")), set())
        days = int(r.days_now) if pd.notna(r.days_now) else 0
        ev = evaluate(dx_code, cset, days, groups)
        cur_fee = float(fee_map.get(getattr(r, src_col("hospital_no")), 0) or 0)
        zzl_raw = zzl_raw_map.get(getattr(r, src_col("hospital_no")))
        zzl_passed = zzl_pass_map.get(getattr(r, src_col("hospital_no")))

        matched = ev["matched"]
        grp = eng.group(main_dx=dx_yb or dx_code, ops=oset, other_dx=dx_other,
                        tcm_classes=cset, days=days)
        g = grp.get("drg")
        w = eng.weight_of(g)
        std = eng.payment_standard(g)
        path = grp["path"]

        if path == PATH_TCM:
            gname = eng.drg_name_of(g) or (groups.get(g) or {}).get("name", "")
            line1 = (f"命中**中医优势病组 {g}**（{gname}）权重 {w} 支付标准 {std:,.2f} 元"
                     if std else f"命中**中医优势病组 {g}**（{gname}）")
            basis = BASIS_TCM
        elif path == PATH_STD and std:
            line1 = (f"标准 CHS-DRG **{g}**（{grp.get('adrg_name')}）"
                     f"权重 {w} 支付标准 {std:,.2f} 元")
            basis = ("标准 ADRG（按已录手术）" if oset
                     else "标准 ADRG（暂按主诊断判内科组，如有手术将重新分组）")
        elif path == PATH_STD:
            line1 = f"标准 CHS-DRG **{g}**（{grp.get('adrg_name')}）无官方权重，支付标准待补"
            basis = "标准 ADRG（无权重）"
        else:
            line1 = f"暂未产出组码：{grp.get('reason') or '数据不足'}"
            basis = BASIS_NONE

        # ①行 states the predicted group and payment standard - W17, and only if confirmed
        line1 = guard.guard("W17", line1, "（分组陈述未获准输出）", where=getattr(r, src_col("hospital_no")))

        delta = round(cur_fee - std, 2) if std else None
        # 中治率 is a hard settlement condition: below 60% the TCM group is settled at the
        # plain medical ADRG weight instead.
        zzl_txt = "—"
        if pd.notna(zzl_raw):
            mark = "达标" if zzl_passed else "**未达标（中医组将降级为内科组）**"
            zzl_txt = f"{zzl_raw:.1%} {mark}"
        rows.append({
            src_col("hospital_no"): getattr(r, src_col("hospital_no")),
            "doctor": getattr(r, src_col("doctor_residency")),
            "doctor_phone": doc_phone,
            "office": getattr(r, src_col("office_now")),
            "enter": getattr(r, src_col("time_enter")),
            "days": days,
            "dx": dx_code,
            "dx_name": (rec[src_col("note")] if rec is not None else None),
            "ops": adv_name.get(getattr(r, src_col("hospital_no"))) or op列(name).get(getattr(r, src_col("serial")), ""),
            "op_classes": ",".join(sorted(cset)),
            "中治率": (round(float(zzl_raw), 4) if pd.notna(zzl_raw) else None),
            "中治率判定": zzl_txt,
            "分组路径": basis,
            "L1_当前预分组": line1,
            "group": g,
            "weight": w,
            "standard": std,
            "fee": cur_fee,
            "delta": delta,
            "L2_入组差距": _gap_rule(guard, _gap_text(ev, dx_code), getattr(r, src_col("hospital_no"))),
            "L3_费用预判": guard.guard("W18", ((f"预计盈利 {-delta:,.2f} 元" if delta < 0
                                               else f"已超支付标准 {delta:,.2f} 元")
                                              + (f"；中治率 {zzl_raw:.1%}" if pd.notna(zzl_raw) else ""))
                                     if delta is not None else "—", "—",
                                     where=getattr(r, src_col("hospital_no"))),
            "L4_优化空间": _advice_rule(guard, _advice(ev, matched, dx_code, cset),
                                    getattr(r, src_col("hospital_no"))),
        })

    df = pd.DataFrame(rows)
    if unmapped:
        print(f"[!] 医师工号不在 sys_doctor（手机号未映射，需运行 s22 或补花名册）："
              f"{sorted(unmapped)}")
    if args.doctor:
        df = df[df["doctor"].astype(str).str.startswith(str(args.doctor))]
    df = df.sort_values(["doctor", "days"], ascending=[True, False])

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    today = args.date or date.today().isoformat()
    csv_path = OUT_SUB / f"insim_{today}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # ---- daily list, grouped by physician ---------------------------
    log = [
        f"# 在院 DRG 模拟器 · 住院医师每日清单（{today}）",
        "",
        f"> {DISCLAIMER}。本清单仅提示「已录入信息是否满足入组条件」，不构成临床建议。",
        "",
        f"- 在院患者：**{len(df)}** 例"
        + (f"（医师 {args.doctor}）" if args.doctor else "（全院）"),
        f"- **中医优势病组**：**{int((df['分组路径'] == BASIS_TCM).sum())}** 例"
        f"　|　**标准 CHS-DRG 预分组**：**{int(df['分组路径'].str.startswith('标准').sum())}** 例"
        f"　|　**暂未产出组码**：**{int((df['分组路径'] == BASIS_NONE).sum())}** 例",
        f"  - 未产出组码的 {int((df['分组路径'] == BASIS_NONE).sum())} 例中，"
        f"**{int(df['dx'].isna().sum())} 例为在院主要诊断尚未录入**（早期住院，录入后即可推演），"
        f"其余为诊断/手术数据不足",
        f"- 参数：费率 {FEE_RATE:,.2f}、机构系数 {COEF}、权重取官方 **{eng.n_groups}** 组目录"
        "「二三级医疗机构权重」",
        f"- **输出合规闸门**（docs/26 白名单）："
        f"{'已加载' if guard.available else '不可读 → 受控输出全部抑制'}"
        f"，本次抑制 **{len(output_guard.suppressed())}** 条",
        "",
    ]
    sup = output_guard.suppressed()
    if len(sup):
        agg = sup.groupby(["rule_id", "reason"]).size().reset_index(name="条数")
        log += ["", "### 被抑制的输出（未获白名单确认或命中黑名单）", "",
                "| 白名单 | 原因 | 条数 |", "|---|---|---:|"]
        for r in agg.itertuples():
            log.append(f"| {r.rule_id} | {r.reason} | {int(r.条数):,} |")
        log.append("")
    for doc, sub in df.groupby(df["doctor"].astype(str)):
        log += [f"## 经管医师：{doc}", "",
                "| 住院号 | 住院天数 | 主要诊断 | 已执行中医操作 | 中治率 | ①当前预分组 | ②入组差距 | ③费用预判 | ④优化空间 |",
                "|---|---:|---|---|---:|---|---|---|---|"]
        for _, r in sub.iterrows():
            log.append(
                f"| {r[src_col("hospital_no")]} | {r['days']} | "
                f"{r['dx'] or '-'} {str(r['dx_name'] or '')[:10]} | "
                f"{str(r['ops'])[:28]} | {r['中治率判定']} | {r['L1_当前预分组']} | "
                f"{str(r['L2_入组差距'])[:70]} | {r['L3_费用预判']} | {r['L4_优化空间']} |")
        log.append("")

    log += ["---", "", "## 合规声明", "",
            "- 白名单：主要诊断/主要操作的正确选择与顺序、漏编中医操作与 CC/MCC 的补录提醒、中医病证规范填报；",
            "- **黑名单（永不生成）**：建议增加诊疗项目、延长住院、改诊断以入更高权重组；",
            "- 每条提示可回溯至政策出处（policy_version=`某地区医保局〔2025〕27号-2025版`）。",
            "",
            "## 数据源与口径说明（v1，2026-09-20 更新）", "",
            "1. **在院「已执行中医操作」已打通**：改取**医嘱表** `源表(advice_long)/Temporary`（`列(time_execute)` 非空 = 已执行），"
            "经 `config/dict/cost_item_attribute.csv` 映射为 27 号文操作类（0 正骨 / 1 推拿 / 2 针法 / 3 灸法 / 4 敷贴薰洗）。"
            "回测验证：用医嘱口径重算入组，召回 **99.8%**（与病案手术编码口径的 S05 基线持平）。",
            "2. **中治率已可计算**：按 27 号文公式（分子＝中医治疗费+中成药费+中药饮片费）逐例汇总，"
            "低于 60% 的病例将在结算时降级为普通内科组权重，故在③行并列提示。",
            "   分母/分子口径来自 `docs/16` 的科目映射；**中医正骨类项目在 HIS 计入统计码 11（手术费）**，"
            "是否计入分子待医保办确认（已列入审核表）。",
            "3. 未命中中医优势病组时的**普通 ADRG 主分组与 CC/MCC 判定**待二期引擎 v1 补齐；",
            "4. 在院主诊断取 `列(kind_input) 1/2`（入院/修正），与出院口径（3/4）可能不同，需追踪修正诊断对预分组的影响；",
            "5. `列(doctor_residency)` 实为「工号|姓名」格式，且存在空值，上线前需与信息科确认工号口径与必填控制；",
            "6. 操作类映射中「仅关键字判定」的收费项目（见 S09 审核表）尚未经医务科确认，属暂用口径。",
            ]

    md = "\n".join(log)
    (OUT_SUB / f"s06_insim_{today}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[完成] 明细 → {csv_path}")
    return 0


def _gap_rule(guard, text: str, hospital_no: str) -> str:
    """②行: the day-gap wording falls under W04 (explicitly non-intervention), else W01.

    W04 does not allow a bare day-gap statement - it requires stating the fact AND disclaiming
    intervention, and the guard rejects it otherwise. So the disclaimer is part of the message,
    not an afterthought: without it "还差 7 天" reads as advice to extend the stay (B02).
    """
    if "住院天数不足" in text:
        rule = "W04"
        if "不做人为干预" not in text:
            text += "；请按临床规范执行，不做人为干预"
    else:
        rule = "W01"
    return guard.guard(rule, text, "—", where=hospital_no)


def _advice_rule(guard, text: str, hospital_no: str) -> str:
    """④行: choose the rule by what the advice actually says."""
    if "病证编码" in text:
        rule = "W02"
    elif "补录" in text:
        rule = "W03"
    else:
        rule = "W01"
    return guard.guard(rule, text, "—", where=hospital_no)


def _gap_text(ev: dict, dx: str | None = None) -> str:
    """Line 2: gaps, but only for groups whose diagnosis list already matches."""
    if ev["matched"]:
        return "已满足入组条件（诊断 + 操作类覆盖 + 住院天数）"
    if not dx:
        return "在院主要诊断尚未录入（西医诊断 列(kind_input) 1/2 且 列(order)=1）→ 录入后重新推演"
    if not ev["near"]:
        return "主要诊断未命中 10 个中医优势病组诊断清单（共 194 个编码）→ 按当前信息将入普通内科组"
    parts = []
    for n in sorted(ev["near"], key=lambda x: len(x["miss"]))[:2]:
        parts.append(f"{n['drg']}（{n['name']}）：{'；'.join(n['miss'])}")
    return " ｜ ".join(parts)


def _advice(ev: dict, matched: list[str], dx: str | None, ops: set[str]) -> str:
    """Line 4: strictly whitelist-only suggestions."""
    if matched:
        return "入组条件已满足；请确认中医病证编码按规范填报"
    if not dx:
        return "请在病程记录中完成在院主要诊断录入（本提示仅涉及已录入信息的完整性）"
    if not ev["near"]:
        return "请由病案编码员复核主要诊断选择依据（本提示仅针对主要诊断的选择，不涉及诊疗项目增减）"
    tips = []
    for n in ev["near"]:
        if any("住院天数不足" in m for m in n["miss"]):
            continue
        if n["lacking_op_groups"]:
            tips.append(f"若临床已执行但未录入，可补录「中医操作类 {n['lacking_op_groups']}」对应操作编码"
                        f"（{n['drg']} 需住院 ≥{n['days_min']} 天且覆盖 ≥{n['need']} 类中医操作，"
                        f"当前 {n['covered']} 类）")
    if not tips:
        tips.append("当前已满足诊断条件，差距在住院天数；请按临床规范执行，不做人为干预")
    return "；".join(tips[:2])


if __name__ == "__main__":
    raise SystemExit(main())
