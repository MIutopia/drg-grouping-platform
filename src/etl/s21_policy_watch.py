"""S21 Policy document watch: fingerprint the files, extract the facts, diff against the system.

docs/24 step 1 assumed a PUSH channel - the bureau issues a document, the hospital logs it,
someone interprets it. In practice that channel does not exist: the bureau publishes documents
and the hospital has to go and find them, so a "radar + incoming-document register" has no feed
to sit on.

This job replaces it with a PULL channel that is reproducible:

    drop the new file into 原始资料/  ->  run s21  ->  it tells you what changed

For every policy file it records a fingerprint (sha256 of the bytes), extracts what it can
(文号, 施行日期, 组数, 中医病种数, 阈值 ...), and diffs those facts against what the system
currently runs on - the drg dictionaries and parameters. The register is therefore a by-product
of the scan: nobody maintains a log by hand, and nothing depends on being told a file arrived.

**What is automated and what is not.** sha256 detects that a file changed - that is reliable and
is the primary signal. The extracted facts only say *what the change might be*; they narrow the
reading, they do not replace it. Section 五 of the report lists explicitly what still needs a
human to read, so the job never pretends to have understood a document.

The second channel needs no documents at all: S20's monthly agreement series reveals a payer-side
rule change that was never published. Both channels are needed, and neither depends on a feed.
Docs: docs/24-政策变更与DRG规则调整应对机制.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR, RAW_DIR  # noqa: E402

OUT_SUB = OUT_DIR / "policy"
DRG = "drg"
POLICY_VERSION = "政策文件台账-v1"
SCAN_DIR = RAW_DIR / "官方下发文件"

EXTS = {".pdf", ".docx", ".xlsx"}

# Facts read out of the text: (fact_key, label, unit, keyword, number pattern, plausible range).
# Matching is KEYWORD-PROXIMITY rather than one big regex, because in Chinese policy text the
# number may sit on either side of the keyword - "从DRG区域预算中预留5%作为DRG付费风险金预算"
# puts 5% *before* 风险金, and a one-directional pattern picked up an unrelated 50 instead.
# The range guard is what keeps a stray number from being reported as policy.
TEXT_FACTS = [
    ("tcm_advantage_n", "中医优势病种数（附件1）", "个", "遴选确定", r"(\d+)个中医优势病种", (1, 300)),
    ("tcm_feature_n", "中医特色病种数（附件4）", "个", "中医特色病种",
     r"(\d+)个中医优势病种", (1, 300)),
    ("tcm_group_n", "中医优势病组数（附件2）", "个", "中医DRG病组", r"(\d+)个", (1, 200)),
    ("tcm_ratio_pct", "中治率硬门槛", "%", "中治率", r"(\d+(?:\.\d+)?)%", (0, 100)),
    # 只认"N日内上传"这种紧贴写法：单看关键词附近会抓到无关的"7个工作日"之类
    ("upload_days", "上传结算清单时限", "日", "上传", r"(\d+)日[^，。]{0,8}上传", (1, 90)),
    ("risk_fund_pct", "风险金预留比例", "%", "风险金", r"(\d+(?:\.\d+)?)%", (0, 100)),
    ("high_rate_pct", "费用极高病例占比", "%", "费用极高", r"(\d+(?:\.\d+)?)%", (0, 100)),
    ("low_rate_pct", "费用极低病例占比", "%", "费用极低", r"(\d+(?:\.\d+)?)%", (0, 100)),
    ("single_case_permille", "特病单议比例", "‰", "单议", r"(\d+(?:\.\d+)?)‰", (0, 100)),
    ("tcm_tilt_pct", "中医特色病种倾斜上限", "%", "倾斜", r"(\d+(?:\.\d+)?)%", (0, 100)),
]

# How a changed fact maps onto docs/24's six categories, and what it costs to apply.
CLASSIFY = {
    "group_n": ("③", "分组方案类", "`dict_group_catalog` / `dict_qy_group`",
                "全量回测 + 影子运行一个月", "**双主管** + 院长知会"),
    "qy_n": ("③", "分组方案类", "`dict_qy_group`", "全量回测 + 影子月", "**双主管** + 院长知会"),
    "tcm_advantage_n": ("②", "中医政策类", "`dict_tcm_group` / `_dx` / `_op`",
                        "中医组回测 + 模拟器验证", "**双主管**"),
    "tcm_feature_n": ("②", "中医政策类", "`dict_tcm_group` / 白名单",
                      "中医组回测 + 白名单复审", "**双主管**"),
    "tcm_group_n": ("②", "中医政策类", "`dict_tcm_group` / `_dx` / `_op`",
                    "中医组回测 + 模拟器验证", "**双主管**"),
    "tcm_ratio_pct": ("①", "参数类", "`param_settlement`", "引擎重算 + 对表", "项目组自验"),
    "upload_days": ("①", "参数类", "`param_settlement`", "引擎重算 + 对表", "项目组自验"),
    "risk_fund_pct": ("①", "参数类", "`param_settlement`（当前**未纳管**）",
                      "引擎重算 + 对表", "项目组自验"),
    "high_rate_pct": ("①", "参数类", "`param_settlement`", "引擎重算 + 对表", "项目组自验"),
    "low_rate_pct": ("①", "参数类", "`param_settlement`", "引擎重算 + 对表", "项目组自验"),
    "single_case_permille": ("①", "参数类", "`param_settlement`（当前**未纳管**）",
                             "引擎重算 + 对表", "项目组自验"),
    "tcm_tilt_pct": ("②", "中医政策类", "`dict_tcm_group` / 白名单",
                     "中医组回测 + 白名单复审", "**双主管**"),
}
DEFAULT_CLASSIFY = ("—", "待人工判读", "待人工判读", "待人工判读", "待人工判读")

# Facts parsed from structure rather than matched by regex: trustworthy enough to raise an alarm.
STRUCTURAL_KEYS = {"group_n", "qy_n"}

# What each fact should equal if the system is up to date. A getter returning None means the
# system does not manage this value at all - reported as 未纳管 rather than silently ignored.
CURRENT_GETTERS = {
    "group_n": ("SELECT COUNT(1) AS v FROM dict_group_catalog", 1.0),
    "qy_n": ("SELECT COUNT(1) AS v FROM dict_qy_group", 1.0),
    "tcm_group_n": ("SELECT COUNT(1) AS v FROM dict_tcm_group", 1.0),
    # 病种数：系统只存病组与诊断清单，不存"病种"个数，无同口径值
    "tcm_advantage_n": (None, None),
    "tcm_ratio_pct": ("SELECT param_value AS v FROM param_settlement "
                      "WHERE param_key = 'tcm_ratio_threshold'", 100.0),
    "upload_days": ("SELECT param_value AS v FROM param_settlement "
                    "WHERE param_key = 'list_upload_limit_days'", 1.0),
    # 极高/极低：文档给的是"病例占比 %"、参数表里是"倍率"，口径不同，硬比会误报
    "high_rate_pct": (None, None),
    "low_rate_pct": (None, None),
    "risk_fund_pct": ("SELECT param_value AS v FROM param_settlement "
                      "WHERE param_key = 'risk_fund_pct'", 1.0),
    "single_case_permille": ("SELECT param_value AS v FROM param_settlement "
                             "WHERE param_key = 'single_case_permille'", 1.0),
    "tcm_tilt_pct": (None, None),
    "tcm_feature_n": (None, None),
}


# ------------------------------------------------------------------
# File handling
# ------------------------------------------------------------------
def discover() -> list[Path]:
    """Policy files on disk: the 下发文件 directory plus anything dropped at the top level."""
    found: set[Path] = set()
    for d in (SCAN_DIR, RAW_DIR):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in EXTS:
                found.add(p.resolve())
    return sorted(found)


def doc_key_of(path: Path) -> str:
    """Stable key for a document, ignoring copy markers like '(7)'."""
    stem = re.sub(r"\(\d+\)\s*$", "", path.stem).strip()
    return re.sub(r"\s+", "", stem)[:60]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text(path: Path) -> tuple[str, str | None]:
    """Return (normalised text, note). Text is whitespace-stripped for regex matching."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader  # noqa: PLC0415
            r = PdfReader(str(path))
            raw = "\n".join((p.extract_text() or "") for p in r.pages)
            if len(raw.strip()) < 200:
                return "", "PDF 无文本层（疑似扫描件），无法自动抽取，须人工通读"
            flat = re.sub(r"\s+", "", raw)
            # Extraction interleaves page numbers into sentences - "...支付标准-13-40%（含）的
            # 入组病例" - which silently corrupts any number read near a keyword.
            return re.sub(r"(?<=\S)-\d{1,3}-(?=\S)", "", flat), None
        if ext == ".docx":
            from docx import Document  # noqa: PLC0415
            d = Document(str(path))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" ".join(c.text.strip() for c in row.cells))
            return re.sub(r"\s+", "", "\n".join(parts)), None
    except Exception as e:  # noqa: BLE001
        return "", f"文本抽取失败：{str(e)[:60]}"
    return "", None          # xlsx: no prose, structural facts only


def structural(path: Path) -> dict:
    """Size/shape figures: pages (pdf), tables (docx), rows (xlsx)."""
    out: dict = {}
    try:
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader  # noqa: PLC0415
            out["pages"] = len(PdfReader(str(path)).pages)
        elif path.suffix.lower() == ".docx":
            from docx import Document  # noqa: PLC0415
            d = Document(str(path))
            out["tables_n"] = len(d.tables)
            out["rows_n"] = sum(len(t.rows) for t in d.tables)
        elif path.suffix.lower() == ".xlsx":
            from openpyxl import load_workbook  # noqa: PLC0415
            wb = load_workbook(path, read_only=True)
            out["tables_n"] = len(wb.worksheets)
            out["rows_n"] = sum((ws.max_row or 0) for ws in wb.worksheets)
            wb.close()
    except Exception as e:  # noqa: BLE001
        out["note"] = f"结构读取失败：{str(e)[:50]}"
    return out


def group_counts(path: Path) -> dict:
    """Real-group and QY counts straight out of a catalogue docx, via S03's own parser."""
    if path.suffix.lower() != ".docx":
        return {}
    try:
        from docx import Document  # noqa: PLC0415
        from s03_extract_group_catalog import parse_group_catalog  # noqa: PLC0415
        doc = Document(str(path))
        rows = []
        for ti, t in enumerate(doc.tables):
            for r in t.rows:
                cells = [c.text.strip().replace("\n", " ") for c in r.cells]
                rows.append([f"T{ti}"] + cells)
        if not rows:
            return {}
        n_col = max(len(r) for r in rows)
        df = pd.DataFrame([r + [""] * (n_col - len(r)) for r in rows])
        t, _capt, qy = parse_group_catalog(df)
        if len(t):
            return {"group_n": len(t), "qy_n": len(qy)}
    except Exception as e:  # noqa: BLE001
        return {"note": f"目录解析失败：{str(e)[:50]}"}
    return {}


# ------------------------------------------------------------------
# Facts
# ------------------------------------------------------------------
def text_facts(text: str) -> dict:
    """Extract facts by keyword proximity, dropping anything outside the plausible range."""
    out = {}
    for key, _label, _unit, kw, pat, (lo, hi) in TEXT_FACTS:
        for m in re.finditer(kw, text):
            seg = text[max(0, m.start() - 24): m.end() + 24]   # number may be before or after
            g = re.search(pat, seg)
            if not g:
                continue
            try:
                v = float(g.group(1))
            except (TypeError, ValueError):
                continue
            if lo <= v <= hi:
                out[key] = g.group(1)
                break
    return out


def current_value(key: str) -> tuple[str | None, bool]:
    """(current value as text, managed?) - managed=False means the system does not hold it."""
    spec = CURRENT_GETTERS.get(key)
    if spec is None or spec[0] is None:
        return None, False
    sql, scale = spec
    try:
        d = ddlio.query(sql, DRG)
        if d.empty:
            return None, False
        v = d.iloc[0]["v"]
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None, False
        f = float(v)
        if scale:
            f *= scale
        return (str(int(f)) if float(f).is_integer() else f"{f:g}"), True
    except Exception:  # noqa: BLE001
        return None, False


def compare(key: str, doc_val: str) -> tuple[str, str, str]:
    """Return (current text, status, comment)."""
    cur, managed = current_value(key)
    if not managed:
        return "—", "仅登记", "系统内无同口径对应值——或属未纳管，或口径不同，不做数值比对"
    try:
        same = abs(float(doc_val) - float(cur)) < 1e-6
    except (TypeError, ValueError):
        return cur, "待人工判读", "文档值与现行值无法数值比对"
    return cur, ("一致" if same else "**不一致**"), ("" if same else "文档值与系统现行值不同")


# ------------------------------------------------------------------
# Output
# ------------------------------------------------------------------
def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def label_of(key: str) -> str:
    for k, lab, _u, _kw, _p, _r in TEXT_FACTS:
        if k == key:
            return lab
    return {"group_n": "细分组目录有效组数", "qy_n": "QY 伪组码数"}.get(key, key)


def report(docs: list[dict], facts: list[dict], changes: list[str],
           unreadable: list[tuple[str, str]]) -> str:
    reg = pd.DataFrame([{k: d[k] for k in
                         ("doc_key", "file_name", "ext", "sha256", "pages", "tables_n",
                          "rows_n", "doc_no", "effective_date", "category")}
                        for d in docs])
    lines = ["# S21 政策文件台账与变更影响评估", "",
             f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
             f"- 扫描目录：`{SCAN_DIR}` 与 `原始资料/`（新增文件直接放入即可）",
             "- **机制说明**：医保侧没有推送通道（文档由医院自行查阅），故本作业以"
             "**文件指纹 + 事实抽取 + 与现行值比对**替代\"收文登记\"——台账是扫描的副产物，"
             "无需人工维护。", "",
             "## 一、政策文件台账", "", _md(reg.fillna("—")), ""]

    lines += ["## 二、与上次扫描的差异（按 sha256）", ""]
    lines += [f"- {c}" for c in changes] if changes else ["- 无（首次扫描，或文件均未变动）。"]
    lines.append("")

    fac = pd.DataFrame(facts)
    lines += ["## 三、文档抽取事实 vs 系统现行值", "",
              "> 「未纳管」= 文档里有但系统内无对应参数；这类就是**需要补进 `param_settlement` 的缺口**。",
              ""]
    if len(fac):
        show = fac[["fact_label", "fact_value", "current", "status", "source"]].copy()
        show.columns = ["事实", "文档值", "系统现行值", "判定", "来源文件"]
        lines += [_md(show.fillna("—")), ""]
    else:
        lines += ["- 未检出可比对事实。", ""]

    diff = fac[fac["status"] == "**不一致**"] if len(fac) else fac
    lines += ["## 四、变更影响评估单（草稿）", ""]
    if len(diff):
        lines += ["| 事实 | 文档值 | 现行值 | 变更类别 | 需更新的表 | 需重跑 | 签认 |",
                  "|---|---|---|---|---|---|---|"]
        for r in diff.itertuples():
            cat, _name, tables, rerun, sign = CLASSIFY.get(r.fact_key, DEFAULT_CLASSIFY)
            lines.append(f"| {r.fact_label} | {r.fact_value} | {r.current} | {cat} | {tables} "
                         f"| {rerun} | {sign} |")
    else:
        lines += ["- **无不一致项**：现行字典/参数与在库政策文件一致，本次无需变更。", ""]
    lines.append("")

    lines += ["## 五、仍需人工通读的部分（本作业不替代）", ""]
    if unreadable:
        for name, note in unreadable:
            lines.append(f"- **{name}**：{note}")
    if len(fac):
        for k, g in fac.groupby("fact_key"):
            vals = sorted(set(g["fact_value"].astype(str)))
            if len(vals) > 1:
                lines.append(f"- **{label_of(k)}**：不同文档给出不同值（{' / '.join(vals)}）"
                             "——至少一个抽取有误，需人工确认以哪份为准")
        todo = fac[fac["status"] == "**待人工确认**"]
        for r in todo.itertuples():
            lines.append(f"- **{r.fact_label}**：仅一份文档检出 {r.fact_value}，"
                         f"系统现行 {r.current}——单源文本抽取不足以判定，需人工核对原文")
    lines += ["- 所有**扫描件 PDF**（无文本层）无法自动抽取；",
              "- 事实抽取只覆盖上表所列项目，**未覆盖的政策条款仍需人工判读**；",
              "- 文档**新增/删除**只能靠 sha256 提示\"有变化\"，**变了什么**仍需对照阅读。", ""]

    lines += ["## 六、两条独立通道", "",
              "| 通道 | 依赖 | 发现什么 |",
              "|---|---|---|",
              "| 本作业（文档侧，主动） | 有人把文件放进 `原始资料/` | 政策文件**明文**变化 |",
              "| `s20_regress.py`（数据侧，被动） | 每月官方结算返回 | 医保侧**隐性**规则变化（不发文、只体现在结算结果里） |",
              "",
              "> 二者互补：文档侧覆盖\"发了文我们没看到\"，数据侧覆盖\"改了但没发文\"。", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只扫描与出报告，不写库")
    a = ap.parse_args()
    OUT_SUB.mkdir(parents=True, exist_ok=True)

    if not a.dry:
        ok = True
        try:
            ddlio.query("SELECT TOP 1 id FROM dict_policy_doc", DRG)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            print("[!] drg.dict_policy_doc 不存在。请先执行：")
            print("    $env:DRG_ALLOW_DDL=1; python src/etl/s18_create_drg_db.py "
                  "--sql 07_policy_watch.sql")
            return 2
        if os.environ.get("DRG_ALLOW_DDL", "0") != "1":
            print("[!] 写库需要 DRG_ALLOW_DDL=1（ddlio 写入门禁）。"
                  "只查看报告：python src/etl/s21_policy_watch.py --dry")
            return 2

    print("[s21] 扫描政策文件 ...")
    files = discover()
    docs, facts, unreadable = [], [], []
    for p in files:
        rel = p.relative_to(RAW_DIR)
        text, note = extract_text(p)
        if note:
            unreadable.append((p.name, note))
        # structural figures describe the FILE (pages/tables/rows) and belong in the register,
        # not in the fact comparison - putting them there buried the policy facts in noise
        st = structural(p)
        gc = group_counts(p)
        if "note" in st:
            unreadable.append((p.name, st["note"]))
        if "note" in gc:
            unreadable.append((p.name, gc["note"]))
        f = text_facts(text)
        f.update({k: str(v) for k, v in gc.items() if k != "note"})

        no = re.search(r"(渝医保[办发]*〔\d{4}〕\d+号)", text)
        eff = re.search(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起(?:施行|执行)", text)
        key = doc_key_of(p)
        docs.append({
            "doc_key": key, "file_name": p.name, "rel_path": str(rel), "ext": p.suffix.lower(),
            "sha256": sha256_of(p)[:16], "size_bytes": p.stat().st_size,
            "pages": st.get("pages"), "tables_n": st.get("tables_n"), "rows_n": st.get("rows_n"),
            "doc_no": no.group(1) if no else "",
            "effective_date": (f"{eff.group(1)}-{int(eff.group(2)):02d}-"
                               f"{int(eff.group(3)):02d}") if eff else "",
            "category": (CLASSIFY.get(next(iter(f), ""), DEFAULT_CLASSIFY)[0]
                         if f else "—"),
        })
        for k, v in f.items():
            cur, status, _c = compare(k, v)
            facts.append({"doc_key": key, "fact_key": k, "fact_label": label_of(k),
                          "fact_value": v, "current": cur, "status": status,
                          "source": key[:26]})

    # ---- confidence grading -----------------------------------------
    # Free-text numeric extraction from these PDFs is genuinely unreliable: page numbers are
    # interleaved into sentences, and the same keyword sits next to unrelated figures. Two
    # guards keep a bad reading from turning into a false alarm:
    #   1. two documents disagreeing  -> neither is trusted, ask a human;
    #   2. a value from a SINGLE free-text source -> never auto-judged as 不一致. Structural
    #      facts (parsed from the catalogue docx, not regex'd) and values confirmed by two
    #      sources are the only ones allowed to raise 不一致.
    seen: dict[str, set[str]] = {}
    cnt: dict[str, int] = {}
    for f in facts:
        seen.setdefault(f["fact_key"], set()).add(str(f["fact_value"]))
        cnt[f["fact_key"]] = cnt.get(f["fact_key"], 0) + 1
    for f in facts:
        k = f["fact_key"]
        if len(seen[k]) > 1:
            f["status"] = "**文档间冲突**"
            f["current"] = "—"
            continue
        trusted = k in STRUCTURAL_KEYS or cnt[k] >= 2
        if not trusted:
            if f["status"] == "一致":
                f["status"] = "一致（单源）"
            elif f["status"] == "**不一致**":
                f["status"] = "**待人工确认**"

    # ---- diff against the previous scan ------------------------------
    changes: list[str] = []
    try:
        prev = ddlio.query("SELECT doc_key, sha256 FROM dict_policy_doc d WHERE captured_at = "
                           "(SELECT MAX(captured_at) FROM dict_policy_doc)", DRG)
    except Exception:  # noqa: BLE001
        prev = pd.DataFrame()
    if len(prev):
        old = {str(r.doc_key): str(r.sha256) for r in prev.itertuples()}
        new = {d["doc_key"]: d["sha256"] for d in docs}
        for k in sorted(set(new) | set(old)):
            if k not in old:
                changes.append(f"**新增**：`{k}`")
            elif k not in new:
                changes.append(f"**移除**：`{k}`")
            elif old[k] != new[k]:
                changes.append(f"**内容变更**：`{k}`（sha256 {old[k][:8]} → {new[k][:8]}）"
                               "→ 需人工比对差异")
        if not changes:
            changes.append("与上次扫描一致，无新增/变更/移除。")
    else:
        changes.append("库内无历史扫描记录，本次为**首次**；自下次起可给出新增/变更/移除。")

    md = report(docs, facts, changes, unreadable)
    (OUT_SUB / "s21_policy_watch.md").write_text(md, encoding="utf-8")
    print(md)

    if a.dry:
        print("[dry] 未写入 drg 库")
        return 0

    now = datetime.now()
    ddlio.load_frame(pd.DataFrame([{**d, "captured_at": now,
                                    "policy_version": POLICY_VERSION} for d in docs])
                     [["doc_key", "file_name", "rel_path", "ext", "sha256", "size_bytes",
                       "pages", "tables_n", "rows_n", "doc_no", "effective_date", "category",
                       "captured_at", "policy_version"]],
                     "dict_policy_doc", truncate=False)
    ddlio.load_frame(pd.DataFrame([{**f, "captured_at": now,
                                    "policy_version": POLICY_VERSION} for f in facts])
                     [["doc_key", "fact_key", "fact_label", "fact_value", "captured_at",
                       "policy_version"]],
                     "dict_policy_fact", truncate=False)
    print(f"    台账 {len(docs)} 份 / 事实 {len(facts)} 条已入库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
