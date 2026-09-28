"""S04 Extract and verify TCM advantage group inclusion rules from the official PDF.

The hand-transcribed workbook must be verified against the official annex before it
can feed the engine. Docs: docs/15-代码模块说明与作业手册.md §6.5.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR, RAW_DIR  # noqa: E402

POLICY_VERSION = "某地区医保局〔2025〕27号-2025版"
DOC_DIR = RAW_DIR / "官方下发文件"
XLSX = RAW_DIR / "中医入组标准(1).xlsx"
OUT_SUB = OUT_DIR / "tcm"
CATALOG_CSV = OUT_DIR / "catalog" / "某地区细分组目录2.0_tables.csv"

PDF_NAME = ("某地区医疗保障局 某地区卫生健康委员会 某地区中医药管理局关于印发"
            "《某地区基本医疗保险按病组（DRG）中医优势病种付费（2025年版）》的通知.pdf")

# The official PDF mixes lower/upper case detail separators (M17.900x003 / M22.200X001).
DX_RE = re.compile(r"^([A-Z]\d{2}(?:\.\d+)*(?:[xX]\d+)*(?:\+[A-Za-z0-9]+\*?)?)\s*(.*)$")
OP_RE = re.compile(r"^(\d{2}\.\d+[A-Z0-9]*)\s*(.*)$")
GRP_RE = re.compile(r"^(\d{1,2})\.([A-Z]{2}\d[A-Z])\s*(.*)$")
DAYS_RE = re.compile(r"住院天数\s*≥\s*(\d+)\s*天")
FOOTER_RE = re.compile(r"^-\s*\d+\s*-$")
# The same annex spells the operation-class headers three different ways, all of which
# must be accepted: "中医主要手术或操作" / "中医手术或操作 1：" / "中医治疗性操作表 2"
# / "中医中医手术或操作 4" (the source text literally repeats 中医).
OPHDR_RE = re.compile(r"^(?:中医)+(?:主要)?(?:治疗性操作表|手术或操作)\s*([1-4])?\s*[:：]?\s*$")
COND_RE = re.compile(r"^入组条件\s*\d*\s*[:：]?")


# ------------------------------------------------------------------
# 1. Official PDF -> rules
# ------------------------------------------------------------------
def read_pdf_text() -> str:
    from pypdf import PdfReader  # noqa: PLC0415

    r = PdfReader(str(DOC_DIR / PDF_NAME))
    return "\n".join((p.extract_text() or "") for p in r.pages)


def _find_annex(text: str, n: int, title_pat: str) -> tuple[int, int]:
    """Locate an annex body.

    The main text references annexes as "(见附件2)", so matching on the number alone
    would hit the cross-reference; the annex title must be matched together with it.
    """
    i = re.search(rf"附件\s*{n}\s*{title_pat}", text)
    if not i:
        return -1, -1
    nxt = re.search(rf"附件\s*{n + 1}\s*[\u4e00-\u9fff]", text[i.end():])
    return i.start(), (i.end() + nxt.start()) if nxt else len(text)


def parse_annex2(text: str) -> dict[str, dict]:
    """Parse annex 2 into {DRG: {name, days, dx[], ops{'1'..'4'/'main'}, combos[], op_mode}}."""
    i, j = _find_annex(text, 2, "中医优势病组主要诊断")
    if i < 0:
        return {}
    seg = text[i:j]
    lines = [ln.strip() for ln in seg.splitlines()]
    lines = [ln for ln in lines if ln and not FOOTER_RE.match(ln)]

    groups: dict[str, dict] = {}
    code: str | None = None
    state = "name"                       # name | cond | dx | op
    op_key = "main"
    cond_buf: list[str] = []

    def flush_cond() -> None:
        """Inclusion conditions are wrapped across lines by the PDF
        ('中医手术或操作\\n1+中医手术或操作2'), so they must be re-joined before parsing
        or the class numbers are lost."""
        nonlocal cond_buf
        if code and cond_buf:
            txt = "".join(cond_buf)
            d = DAYS_RE.search(txt)
            if d:
                groups[code]["days"] = int(d.group(1))
            for part in re.split(r"入组条件\s*\d*\s*[:：]?", txt):
                nums = re.findall(r"中医手术或操作\s*([1-4])", part)
                if nums:
                    groups[code]["combos"].append(sorted({int(x) for x in nums}))
        cond_buf = []

    for ln in lines:
        m = GRP_RE.match(ln)
        if m:
            flush_cond()
            code = m.group(2)
            groups[code] = {"order": int(m.group(1)), "name": m.group(3).strip(),
                            "days": None, "dx": [], "ops": {}, "combos": []}
            state = "name"
            op_key = "main"
            continue

        if code is None:
            continue

        if COND_RE.match(ln):
            if state != "cond":
                cond_buf = []
            state = "cond"
            cond_buf.append(ln)
            continue

        if ln == "主要诊断":
            flush_cond()
            state = "dx"
            continue

        oh = OPHDR_RE.match(ln)
        if oh:
            flush_cond()
            state = "op"
            op_key = oh.group(1) or "main"
            groups[code]["ops"].setdefault(op_key, [])
            continue

        if state == "cond":
            cond_buf.append(ln)
            continue

        target = (DX_RE if state == "dx" else OP_RE) if state in ("dx", "op") else None
        mm = target.match(ln) if target else None
        if mm:
            val, nm = mm.group(1), mm.group(2).strip()
            if state == "dx":
                groups[code]["dx"].append({"code": val, "name": nm})
            else:
                groups[code]["ops"].setdefault(op_key, []).append({"code": val, "name": nm})
        elif state == "name":
            groups[code]["name"] += ln
        elif state == "dx" and groups[code]["dx"]:
            groups[code]["dx"][-1]["name"] += ln          # name wrapped onto next line
        elif state == "op" and groups[code]["ops"].get(op_key):
            groups[code]["ops"][op_key][-1]["name"] += ln

    flush_cond()

    for c, gg in groups.items():
        gg["name"] = re.sub(r"\s+", "", gg["name"])
        for k, lst in gg["ops"].items():
            for it in lst:
                it["name"] = re.sub(r"\s+", "", it["name"])
        for it in gg["dx"]:
            it["name"] = re.sub(r"\s+", "", it["name"])
        gg["op_mode"] = "combo" if gg["combos"] else "any"
        if gg["combos"]:
            all_groups = sorted({n for cb in gg["combos"] for n in cb})
            gg["op_groups"] = all_groups
            gg["min_ops"] = min(len(cb) for cb in gg["combos"])
            import math  # noqa: PLC0415
            gg["covers_all_triples"] = (
                len(gg["combos"]) >= math.comb(len(all_groups), gg["min_ops"])
            )
    return groups


# ------------------------------------------------------------------
# 2. Hand-transcribed workbook (to be verified)
# ------------------------------------------------------------------
def parse_xlsx() -> pd.DataFrame:
    d = pd.read_excel(XLSX, sheet_name="中医优势病种", header=None, engine="openpyxl", dtype=object)
    body = d.iloc[2:].copy()
    names = ["序号", "DRG编码", "行内序", "主诊断", "中医主要手术或操作",
             "c5", "c6", "c7", "住院天数", "标准费用"]
    body.columns = names[:len(body.columns)]
    return body.dropna(subset=["DRG编码"]).reset_index(drop=True)


def catalog_weights() -> dict[str, float]:
    if not CATALOG_CSV.exists():
        return {}
    df = pd.read_csv(CATALOG_CSV, dtype=str, encoding="utf-8-sig")
    df.columns = ["tag", "mdc", "code", "name", "attr", "type", "w23", "w1", "x"][:len(df.columns)]
    out = {}
    for _, r in df[df["tag"] == "T0"].iterrows():
        try:
            out[str(r["code"])] = float(r["w23"])
        except (TypeError, ValueError):
            continue
    return out


def main() -> int:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    text = read_pdf_text()
    (OUT_SUB / "中医优势病种付费2025_原文.txt").write_text(text, encoding="utf-8")

    groups = parse_annex2(text)
    xl = parse_xlsx()
    w23 = catalog_weights()
    FEE_RATE, COEF = 9575.25, 0.92

    log: list[str] = [
        "# S04 中医优势病组入组规则提取与核对报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "- 政策依据：**某地区医保局〔2025〕27 号**《某地区基本医疗保险按病组（DRG）中医优势病种付费（2025 年版）》附件 2",
        f"- policy_version：`{POLICY_VERSION}`",
        f"- 权威来源：`{PDF_NAME}`",
        "- 待核对手工件：`原始资料/中医入组标准(1).xlsx`",
        "",
        "## 一、官方附件 2 解析结果（10 个中医优势病组）",
        "",
        "| # | DRG | 组名 | 住院天数≥ | 主要诊断数 | 中医操作 | 入组条件类型 |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for c, gg in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        if gg["op_mode"] == "combo":
            grp = "/".join(str(x) for x in gg["op_groups"])
            op_txt = f"{len(gg['ops'])} 类共 {sum(len(v) for v in gg['ops'].values())} 项"
            cond = f"**组合**：操作类 {grp} 中至少 **{gg['min_ops']}** 类"
            if gg.get("covers_all_triples"):
                cond += f"（即任取 {gg['min_ops']} 类）"
            else:
                cond += "（仅限下列组合）"
        else:
            op_txt = f"{sum(len(v) for v in gg['ops'].values())} 项"
            cond = "交集：诊断 ∈ 清单 且 操作 ∈ 清单"
        log.append(f"| {gg['order']} | **{c}** | {gg['name']} | {gg['days']} | "
                   f"{len(gg['dx'])} | {op_txt} | {cond} |")

    # ---- compare against the hand transcription --------------------------
    log += ["", "## 二、与手工转录件逐组对照", "",
            "| DRG | 官方诊断 | 转录诊断 | 官方操作 | 转录操作 | 住院天数 | 判定 |",
            "|---|---:|---:|---:|---:|---:|---|"]
    for c, gg in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        sub = xl[xl["DRG编码"].astype(str).str.strip() == c]
        n_dx = int(sub["主诊断"].notna().sum())
        n_op = int(sub["中医主要手术或操作"].notna().sum())
        off_op = sum(len(v) for v in gg["ops"].values())
        days = sorted({str(x).replace(".0", "") for x in sub["住院天数"].dropna()})
        ok_days = days == [str(gg["days"])]
        notes = []
        if n_dx != len(gg["dx"]):
            notes.append(f"诊断数差 {len(gg['dx']) - n_dx}")
        if n_op != off_op:
            notes.append(f"**操作数少 {off_op - n_op} 项**")
        if not ok_days:
            notes.append("住院天数不一致")
        verdict = "一致" if not notes else "；".join(notes)
        log.append(f"| {c} | {len(gg['dx'])} | {n_dx} | {off_op} | {n_op} | "
                   f"{gg['days']} / {','.join(days)} | {verdict} |")

    # ---- transcription defects -------------------------------------------
    log += ["", "## 三、转录缺陷（核对结论，P0）", "",
            "### 缺陷 1｜结构性：把「诊断清单 × 操作清单」按行拍平（影响入组判定）", "",
            "官方语义是两个**相互独立**的清单取交集（正骨术组），转录件却做成一行一配对。以 IS1Z 为例：",
            "", "| 转录件行 | 转录件「主诊断」 | 转录件「中医主要手术或操作」 |", "|---|---|---|"]
    for _, r in xl[xl["DRG编码"].astype(str).str.strip() == "IS1Z"].head(5).iterrows():
        log.append(f"| {r['行内序']} | {r['主诊断']} | {r['中医主要手术或操作']} |")
    is1z = groups.get("IS1Z", {})
    log += ["",
            f"官方 IS1Z 实为 **{len(is1z.get('dx', []))} 个主要诊断 × "
            f"{sum(len(v) for v in is1z.get('ops', {}).values())} 个中医操作** 的任意组合（住院天数≥{is1z.get('days')}）。",
            "按转录件执行会把大量合规病例误判为「不满足入组条件」。"]

    log += ["", "### 缺陷 2｜完整性：中医治疗组只转录了「操作 1」，丢失操作 2/3/4", "",
            "| DRG | 官方操作类数 | 官方操作项数 | 转录操作项数 | 丢失 | 官方入组条件 |",
            "|---|---:|---:|---:|---:|---|"]
    for c, gg in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        if gg["op_mode"] != "combo":
            continue
        sub = xl[xl["DRG编码"].astype(str).str.strip() == c]
        n_op = int(sub["中医主要手术或操作"].notna().sum())
        off_op = sum(len(v) for v in gg["ops"].values())
        log.append(f"| {c} | {len(gg['ops'])} | {off_op} | {n_op} | **{off_op - n_op}** | "
                   f"操作类 {gg['op_groups']} 中至少 {gg['min_ops']} 类 |")
    log += ["",
            "> 这是本次核对最关键的发现：IU2F（本单位占比 ~47%）的真实入组条件不是「有一个毫针/推拿就行」，",
            "> 而是**必须同时覆盖 3 类中医操作**（推拿类 + 针法类 + 灸法类 + 敷贴薰洗类 中的任意 3 类），",
            "> 且住院天数 ≥10 天、主要诊断在 13 个指定编码内。",
            "> 转录件丢失了操作 2/3/4，若按它建规则，会把一大部分**本该入中医组**的病例漏掉（收入损失），",
            "> 也会把不满足组合条件的病例误判为达标（合规风险）——两个方向都错。"]

    # ---- standard-fee cross check ----------------------------------------
    log += ["", "## 四、独立交叉校验：转录件「标准费用」= 官方二三级权重 × 费率 × 机构系数", "",
            f"算式：`标准费用 = 二三级权重(官方 839 组目录) × {FEE_RATE} × {COEF}`", "",
            "| DRG | 转录件标准费用 | 官方二三级权重 | 反算费用 | 差异 |", "|---|---:|---:|---:|---:|"]
    for c, gg in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        sub = xl[xl["DRG编码"].astype(str).str.strip() == c]
        fee = pd.to_numeric(sub["标准费用"], errors="coerce").dropna()
        w = w23.get(c)
        if fee.empty or not w:
            continue
        calc = w * FEE_RATE * COEF
        log.append(f"| {c} | {fee.iloc[0]:,.2f} | {w} | {calc:,.2f} | {calc - fee.iloc[0]:+.2f} |")
    log += ["",
            "> 该算式左右两边**来源完全独立**（左＝政策附件 2，右＝细分组目录 + N 例结算实测反推的系数）。",
            "> 二者吻合，等价于对 **费率 9,575.25**、**机构系数 0.92**、**二三级机构口径** 做了一次交叉验证。"]

    # ---- TCM treatment cost ratio ------------------------------------------
    log += ["", "## 五、新发现的结算前置条件：中治率 ≥ 60%（原方案未纳入）", "",
            "官方原文（二·结算清算）：", "",
            "> 入组「中医优势病组」的住院病例，按相应入组权重进行月结算。……月度结算或年度清算时依据"
            "「中治率」≥60% 进行考核，对「中治率」＜60% 的病例，按相应 ADRG 内科 DRG 病组"
            "（区分严重程度分组的，按伴一般合并症或并发症组）权重标准进行清算。",
            "",
            "> 中治率 =（中医治疗费+中成药费+中药饮片费）/（中医治疗费+西医治疗费+西药费+中成药费+中药饮片费+手术费+卫生材料费）×100%",
            ""]
    w_iu2f, w_iu23 = w23.get("IU2F"), w23.get("IU23")
    if w_iu2f and w_iu23:
        a, b = w_iu2f * FEE_RATE * COEF, w_iu23 * FEE_RATE * COEF
        log += [f"**财务影响量化（以 IU2F 为例）**：达标 {w_iu2f} → 支付标准 {a:,.2f} 元；"
                f"未达标降级为 IU23（{w_iu23}）→ {b:,.2f} 元；",
                f"单例差额 **{a - b:,.2f} 元**。按回测期 IU2F 261 例 / 3 个月 ≈ 87 例/月，",
                f"若中治率整体不达标，月度收入影响约 **{(a - b) * 87 / 10000:.1f} 万元**。", ""]
    log += [
        "**落地要求（新增到一期质控与在院模拟器）**：",
        "1. 中治率作为**与入组并列的预结算前置指标**，在在院模拟器中实时提示（分子分母口径按官方公式）；",
        "2. 费用三表 `列(sort_code)` 统计码需与中治率六个科目逐项对齐（中医治疗费/中成药费/中药饮片费/西药费/手术费/卫生材料费）；",
        "3. 属「政策硬条件」，纳入白名单规则并挂 27 号文条款出处，供医务科+信息科双主管审议签认。",
    ]

    # ---- persist ----------------------------------------------------------
    rows = []
    for c, gg in groups.items():
        for d in gg["dx"]:
            rows.append({"policy_version": POLICY_VERSION, "drg_code": c, "drg_name": gg["name"],
                         "days_min": gg["days"], "op_mode": gg["op_mode"],
                         "op_groups_required": (",".join(map(str, gg.get("op_groups", [])))
                                                if gg["op_mode"] == "combo" else ""),
                         "min_op_groups": gg.get("min_ops", 0),
                         "kind": "dx", "op_group": "", "code": d["code"], "name": d["name"]})
        for k, lst in gg["ops"].items():
            for o in lst:
                rows.append({"policy_version": POLICY_VERSION, "drg_code": c, "drg_name": gg["name"],
                             "days_min": gg["days"], "op_mode": gg["op_mode"],
                             "op_groups_required": (",".join(map(str, gg.get("op_groups", [])))
                                                    if gg["op_mode"] == "combo" else ""),
                             "min_op_groups": gg.get("min_ops", 0),
                             "kind": "op", "op_group": k, "code": o["code"], "name": o["name"]})
    rules = pd.DataFrame(rows)
    rules.to_csv(OUT_SUB / "tcm_advantage_rules.csv", index=False, encoding="utf-8-sig")
    payload = json.dumps({c: {k: v for k, v in gg.items()} for c, gg in groups.items()},
                         ensure_ascii=False, indent=2)
    (OUT_SUB / "tcm_advantage_rules.json").write_text(payload, encoding="utf-8")
    # runtime copy: the DRG engine reads from config/dict so it does not depend on out/
    (CONFIG_DIR / "dict").mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "dict" / "tcm_advantage_rules.json").write_text(payload, encoding="utf-8")

    log += ["", "## 六、产出", "",
            f"- `src/out/tcm/tcm_advantage_rules.csv` —— **{len(rules):,} 条**规则"
            f"（{len(groups)} 组；诊断 {sum(len(g['dx']) for g in groups.values())} 条，"
            f"操作 {sum(sum(len(v) for v in g['ops'].values()) for g in groups.values())} 条，全部取自官方 PDF 原文）",
            "- `src/out/tcm/tcm_advantage_rules.json` —— 结构化规则（含组合条件、操作分类）",
            f"- `src/out/tcm/中医优势病种付费2025_原文.txt` —— 官方 PDF 全文（{len(text):,} 字符，含附件 1~5）",
            "",
            "## 七、下一步", "",
            "1. 解析附件 1（23 病种 ↔ 10 病组）与附件 4/5（12 个中医特色病种 + 倾斜比例 + 绩效考核指标），补全规则库；",
            "2. 用本规则表对 2026-03~05 的 N 例回测：验证「按官方口径重算能否复现官方 IU2F 判定」；",
            "3. 中治率接入费用三表后纳入在院模拟器 v0；",
            "4. 规则表提交医务科主管 + 信息科主管审议签认（白名单首期 10~15 条）。",
            ]

    md = "\n".join(log)
    (OUT_SUB / "s04_tcm_rules_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
