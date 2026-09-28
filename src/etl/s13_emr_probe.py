"""S13 Probe the feasibility of AI-assisted coding (medical-record and order text).

Medical Affairs asked whether missing or wrong diagnosis / operation codes can be recovered
from the narrative record and prescriptions. Answering that requires evidence about what is
actually stored, so this probe measures:
  1. 病历    - record count, document types, and the physical format of 列(word) (image blob);
  2. orders  - long/temporary advice volume and item categories;
  3. recipe  - prescription table volume and usability.
No AI is invoked here; the probe only establishes whether machine-readable text exists.
Docs: docs/20-AI辅助编码可行性评估.md.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402

OUT_SUB = OUT_DIR / "emr"
DAYS_FROM = "2026-02-20"

# Magic bytes of the container formats a Chinese HIS commonly stores narrative records in.
MAGIC = [
    (b"{\\rtf", "RTF 富文本"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE2 复合文档（Word 97-2003 .doc）"),
    (b"PK\x03\x04", "OOXML（.docx/.zip）"),
    (b"<?xml", "XML"),
    (b"\x1f\x8b", "gzip 压缩"),
]


def detect_format(head: bytes) -> str:
    for sig, name in MAGIC:
        if head.startswith(sig):
            return name
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "UTF-16 纯文本"
    return "其他/未知"


def probe_emr() -> tuple[list[str], pd.DataFrame]:
    log: list[str] = []
    total = query_df("SELECT COUNT(1) AS n FROM [病历]").iloc[0, 0]
    rng = query_df("SELECT MIN(列(time_input)) AS mn, MAX(列(time_input)) AS mx FROM [病历]")
    log += ["## 一、病历文书表（`病历`）", "",
            f"- 全表行数：**{int(total):,}**",
            f"- 时间范围：{rng.iloc[0]['mn']} ~ {rng.iloc[0]['mx']}", ""]

    # Document types actually in use
    types = query_df(
        "SELECT TOP 15 列(title_name), COUNT(1) AS n FROM [病历] "
        f"WHERE 列(time_input) >= '{DAYS_FROM}' GROUP BY 列(title_name) ORDER BY COUNT(1) DESC")
    log += [f"### 1.1 文书类型（{DAYS_FROM} 起）", "", _md(types), ""]

    # Linkage to the admission
    link = query_df(
        "SELECT COUNT(1) AS n, COUNT(s_id) AS with_sid, "
        "SUM(CASE WHEN 列(diagnose) IS NOT NULL THEN 1 ELSE 0 END) AS with_dx "
        f"FROM [病历] WHERE 列(time_input) >= '{DAYS_FROM}'")
    log += ["### 1.2 关联与字段填充", "", _md(link), "",
            "> `s_id` 为病历与住院流水号的关联键；`列(diagnose)` 是文书自带的诊断字段。", ""]

    # Physical format of 列(word) - the decisive question for AI extraction
    fmt = query_df(
        "SELECT DATALENGTH(列(word)) AS n_bytes, "
        "SUBSTRING(CONVERT(VARBINARY(MAX), 列(word)), 1, 8) AS head "
        f"FROM [病历] WHERE 列(time_input) >= '{DAYS_FROM}' AND 列(word) IS NOT NULL")
    if fmt.empty:
        log += ["### 1.3 `列(word)` 物理格式", "", "- 区间内无数据。", ""]
        return log, fmt

    fmt["n_bytes"] = pd.to_numeric(fmt["n_bytes"], errors="coerce")
    fmt["格式"] = fmt["head"].map(lambda h: detect_format(bytes(h) if h is not None else b""))
    stat = fmt["n_bytes"].describe()
    log += ["### 1.3 `列(word)` 物理格式（**决定 AI 抽取成本的字段**）", "",
            f"- 样本量：**{len(fmt):,}**（{DAYS_FROM} 起、`列(word)` 非空）",
            f"- 字节数：中位 **{stat['50%']:.0f}**、均值 {stat['mean']:.0f}、"
            f"最小 {stat['min']:.0f}、最大 {stat['max']:.0f}",
            f"- 空/极短（<100 字节）：**{int((fmt['n_bytes'] < 100).sum()):,}**"
            f"（{(fmt['n_bytes'] < 100).mean():.1%}）", ""]
    dist = fmt["格式"].value_counts().rename_axis("物理格式").reset_index(name="例数")
    dist["占比"] = dist["例数"].map(lambda n: f"{n / len(fmt):.1%}")
    log += ["> **字段类型为 `image`（二进制大对象），不是文本列** —— 这是本次评估的核心结论。", "",
            _md(dist), "",
            "> 判读：若为 RTF/OLE2，AI 抽取前必须先做**文档解析**（RTF 解码 / OLE 抽取），"
            "不能直接读字段；若为纯文本则可直接进入文本管线。", ""]
    return log, fmt


def _try(sql: str) -> pd.DataFrame | None:
    """Run a probe query, returning None instead of aborting the whole report."""
    try:
        return query_df(sql)
    except Exception as e:  # noqa: BLE001
        print(f"    [probe] 查询失败：{str(e)[:90]}")
        return None


def probe_orders() -> list[str]:
    log = ["## 二、医嘱表（用药与诊疗执行的来源）", ""]
    for tbl, label in (("源表(advice_long)", "长期医嘱"),
                       ("源表(advice_temp)", "临时医嘱"),
                       ("源表(advice_exec)", "医嘱执行")):
        r = _try(f"SELECT COUNT(1) AS n, COUNT(DISTINCT 列(hospital_no)) AS cases, "
                 f"MAX(列(time_input)) AS latest FROM {tbl} "
                 f"WHERE 列(time_input) >= '{DAYS_FROM}'")
        if r is None:
            log.append(f"- `{tbl}`：查询失败")
            continue
        r0 = r.iloc[0]
        log.append(f"- **{label}** `{tbl}`：{int(r0['n']):,} 行 / {int(r0['cases']):,} 例，"
                   f"最新 {r0['latest']}")
    log.append("")

    # amounts are stored as quantity x unit price, not as a single money column
    items = _try(
        "SELECT TOP 15 列(cost_kind), COUNT(1) AS n, "
        "SUM(列(cost_number) * 列(cost_price)) AS amount "
        f"FROM 源表(advice_long) WHERE 列(time_input) >= '{DAYS_FROM}' "
        "GROUP BY 列(cost_kind) ORDER BY COUNT(1) DESC")
    log += ["### 2.1 长期医嘱的收费类别分布", "",
            _md(items) if items is not None else "- 查询失败", ""]

    herbs = _try(
        "SELECT TOP 15 列(cost_item), COUNT(1) AS n, "
        "SUM(列(cost_number) * 列(cost_price)) AS amount FROM 源表(advice_long) "
        f"WHERE 列(time_input) >= '{DAYS_FROM}' AND (列(cost_item) LIKE '%汤%' "
        "OR 列(cost_item) LIKE '%饮片%' OR 列(cost_item) LIKE '%颗粒%' "
        "OR 列(cost_item) LIKE '%煎%' OR 列(cost_item) LIKE '%方%') "
        "GROUP BY 列(cost_item) ORDER BY COUNT(1) DESC")
    log += ["### 2.2 中药类医嘱（按项目名称关键词命中）", "",
            _md(herbs) if herbs is not None and len(herbs) else "- 未命中中药类医嘱名称。", ""]
    return log


def probe_recipe() -> list[str]:
    log = ["## 三、处方表（`源表(recipe)`）", ""]
    d = _try("SELECT COUNT(1) AS n, MIN(列(time_input)) AS mn, MAX(列(time_input)) AS mx, "
             "COUNT(DISTINCT 列(sql)) AS distinct_sql FROM 源表(recipe)")
    if d is None:
        return log + ["- 查询失败", ""]
    r = d.iloc[0]
    log += [f"- 行数 **{int(r['n']):,}**，时间 {r['mn']} ~ {r['mx']}，"
            f"`列(sql)` 去重后 {int(r['distinct_sql']):,} 条", "",
            "> `列(sql)` 为 `varchar(MAX)`：该表存的是**处方查询语句模板**（配置腔），"
            "不是处方明细，不能作为用药文本来源；用药数据应取医嘱表。", ""]
    return log


def probe_med_record() -> list[str]:
    """med_record is the live EMR database; measure its linkage and content coverage."""
    log = ["## 四、电子病历库 `med_record`（**真实病历文本来源**）", "",
           "> SBO 库内的 `病历` 表已废弃（24 行 / 停在 2018 年）；实际病历在 **`med_record`** 库，"
           "同一实例、当前账号**可读**。", ""]
    r = _try(
        "SELECT COUNT(1) AS records, COUNT(DISTINCT 住院号) AS cases, "
        "MIN(入院时间) AS mn, MAX(入院时间) AS mx "
        "FROM med_record.dbo.源表(med_record)")
    if r is None:
        return log + ["- `med_record` 查询失败。", ""]
    r0 = r.iloc[0]
    log += [f"- 病历文书 **{int(r0['records']):,}** 份，覆盖 **{int(r0['cases']):,}** 例，"
            f"入院时间 {r0['mn']} ~ {r0['mx']}", ""]

    link = _try(
        "SELECT COUNT(DISTINCT m.列(hospital_no)) AS his_cases, "
        "COUNT(DISTINCT r.住院号) AS matched "
        "FROM 源表(admission) m JOIN med_record.dbo.源表(med_record) r "
        f"ON r.住院号 = m.列(hospital_no) WHERE m.列(time_out) >= '{DAYS_FROM}'")
    if link is not None:
        l0 = link.iloc[0]
        his_n = int(l0["his_cases"])
        log += ["### 4.1 与 HIS 出院病例的关联率", "",
                f"- HIS 窗口内出院病例：**{his_n:,}** 例",
                f"- 其中在 `med_record` 有病历文书：**{int(l0['matched']):,}** 例"
                f"（{int(l0['matched']) / his_n:.1%}）" if his_n else "", ""]

    log += ["### 4.2 病历正文与诊断字段填充率（决定 AI 是否\"有米下锅\"）", ""]
    fields = {
        "现病史": "叙事主体", "专科情况": "中医四诊/专科查体", "辅助检查": "检验影像结果",
        "体格检查": "体征", "既往史": "合并症线索", "主诉": "主诉",
        "西医其他诊断_疾病编码": "**CC/MCC 来源**", "中医其他诊断_疾病编码": "中医兼证",
        "西医初步诊断_疾病编码": "西医主诊断候选", "中医初步诊断_疾病编码": "中医主诊断候选",
    }
    rows = []
    for col, note in fields.items():
        q = _try("SELECT COUNT(1) AS n, AVG(LEN(CAST([" + col + "] AS VARCHAR(MAX)))) AS avg_len "
                 "FROM med_record.dbo.源表(med_record) "
                 "WHERE [" + col + "] IS NOT NULL AND LTRIM(RTRIM(CAST([" + col +
                 "] AS VARCHAR(MAX)))) <> ''")
        if q is None:
            continue
        n = int(q.iloc[0]["n"])
        avg = q.iloc[0]["avg_len"]
        rows.append({"字段": col, "含义": note, "非空份数": n,
                     "填充率": f"{n / int(r0['records']):.1%}",
                     "平均字数": round(float(avg), 1) if pd.notna(avg) else 0})
    log += [_md(pd.DataFrame(rows)), ""]
    return log


def probe_homepage() -> list[str]:
    """The disease-case homepage carries the codable fields; measure real coverage."""
    log = ["## 五、病案首页 `med_record.源表(case_man)`（编码质控数据集）", ""]
    total = _try("SELECT COUNT(1) AS n FROM med_record.dbo.源表(case_man)")
    if total is None:
        return log + ["- 查询失败。", ""]
    列(all) = int(total.iloc[0, 0])
    log.append(f"- 首页记录：**{列(all):,}** 条（88 个字段）")
    log.append("")

    keys = [("中医诊断_疾病编码", "中医主诊断编码"), ("西医诊断_疾病编码", "**西医主诊断编码**"),
            ("手术编码", "**手术操作编码**"), ("编码员", "编码员署名"),
            ("住院医师", "住院医师"), ("质控医生", "质控医生"),
            ("住院天数", "住院天数"), ("中医诊疗技术", "中医诊疗技术标志"),
            ("使用医疗机构中药制剂", "中药制剂标志"), ("辨证施护", "辨证施护标志")]
    rows = []
    for col, note in keys:
        q = _try("SELECT COUNT(1) AS n, AVG(LEN(CAST([" + col + "] AS VARCHAR(MAX)))) AS avg_len "
                 "FROM med_record.dbo.源表(case_man) "
                 "WHERE [" + col + "] IS NOT NULL AND LTRIM(RTRIM(CAST([" + col +
                 "] AS VARCHAR(MAX)))) <> ''")
        if q is None:
            continue
        n = int(q.iloc[0]["n"])
        avg = q.iloc[0]["avg_len"]
        rows.append({"字段": col, "含义": note, "非空条数": n,
                     "填充率": f"{n / 列(all):.1%}",
                     "平均字数": round(float(avg), 1) if pd.notna(avg) else 0})
    log += [_md(pd.DataFrame(rows)), "",
            "> 判读：上表即医务科所说「没写」的**量化证据**——西医诊断编码与手术编码的缺口最大，"
            "且「编码员」署名率反映编码工作有无专人覆盖。", ""]
    return log


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None, metavar="住院号",
                    help="导出该例病历文书的首段可读文本（验证文本抽取链路）")
    a = ap.parse_args()
    OUT_SUB.mkdir(parents=True, exist_ok=True)

    if a.dump:
        return dump_one(a.dump)

    log = ["# S13 AI 辅助编码可行性探针", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
           "- 目的：核实病历文书与用药数据是否具备**机器可读文本**，作为 AI 辅助编码的前提。", ""]
    emr_log, _ = probe_emr()
    log += emr_log
    log += probe_orders()
    log += probe_recipe()
    log += probe_med_record()
    log += probe_homepage()

    md = "\n".join(log)
    (OUT_SUB / "s13_emr_probe.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def dump_one(hospital_no: str) -> int:
    """Extract readable text from one admission's records to validate the parsing chain."""
    d = query_df(
        "SELECT 列(title_name), DATALENGTH(列(word)) AS n_bytes, "
        "CONVERT(VARBINARY(MAX), 列(word)) AS blob, 列(time_input) "
        f"FROM [病历] WHERE s_id IN (SELECT s_id FROM [病历] WHERE 1=1) "
        f"AND s_id = '{hospital_no}' ORDER BY 列(time_input)")
    if d.empty:
        # s_id is the medical-record key; fall back to matching the admission number
        d = query_df(
            "SELECT 列(title_name), DATALENGTH(列(word)) AS n_bytes, "
            "CONVERT(VARBINARY(MAX), 列(word)) AS blob, 列(time_input) "
            f"FROM [病历] WHERE s_id LIKE '%{hospital_no}%' ORDER BY 列(time_input)")
    if d.empty:
        print(f"未找到住院号 {hospital_no} 的病历文书")
        return 1
    for r in d.itertuples():
        blob = bytes(r.blob) if r.blob is not None else b""
        head = detect_format(blob[:8])
        text = extract_text(blob)
        print(f"\n=== {getattr(r, src_col("title_name"))} | {r.n_bytes} 字节 | {head} | {getattr(r, src_col("time_input"))}")
        print(f"--- 抽取文本（前 400 字，共 {len(text)} 字）---")
        print(text[:400])
    return 0


RTF_CTRL = re.compile(r"\\'([0-9a-fA-F]{2})|\\[a-zA-Z]+-?\d* ?|[{}]")


def extract_text(blob: bytes) -> str:
    """Best-effort text extraction for RTF and plain-text blobs (no external deps)."""
    if not blob:
        return ""
    if blob.startswith(b"{\\rtf"):
        s = blob.decode("latin-1", errors="replace")
        s = RTF_CTRL.sub(lambda m: _rtf_repl(m), s)
        return re.sub(r"\s+", " ", s).strip()
    if blob.startswith(b"\xd0\xcf\x11\xe0"):
        return "(OLE2 复合文档：需 antiword / libreoffice 转换，本探针不做解析)"
    for enc in ("utf-8", "gbk"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return "(无法解码)"


def _rtf_repl(m: re.Match) -> str:
    if m.group(1):
        try:
            return bytes.fromhex(m.group(1)).decode("gbk")
        except (ValueError, UnicodeDecodeError):
            return ""
    tok = m.group(0)
    if tok.startswith("\\par") or tok.startswith("\\line"):
        return "\n"
    return "" if tok.startswith("\\") else ""


if __name__ == "__main__":
    raise SystemExit(main())
