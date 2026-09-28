"""S03 Extract the official DRG group catalogue from the Chongqing docx notice.

Produces the authoritative group-code dictionary used by the comparison, rules and
simulator jobs, plus the 4th-digit code semantics.
Docs: docs/15-代码模块说明与作业手册.md §6.4.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR, RAW_DIR  # noqa: E402

DOC_DIR = RAW_DIR / "官方下发文件"
OUT_SUB = OUT_DIR / "catalog"
DICT_OUT = CONFIG_DIR / "dict"


def parse_group_catalog(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Split the T0 table into real groups, caption cells and QY pseudo-codes.

    Codes that are not a real group (a caption row, or a QY pseudo-group which carries no
    weight) are split out so the report can show exactly what was filtered, and QY codes are
    kept for the ungrouped-case rule.
    Returns (catalog rows, caption cells, QY codes). Pure - no file is written, so S21's policy
    watch can reuse it just to count what a newly issued document would produce, without
    overwriting the runtime dictionary.
    """
    t = df[df.iloc[:, 0] == "T0"].iloc[:, 1:9].copy()
    t.columns = ["mdc", "drg_code", "drg_name", "drg_attr", "drg_type",
                 "weight_23", "weight_1", "x"]
    code = t["drg_code"].astype(str)
    keep = code.str.match(r"^[A-Z]{2}\d", na=False)
    dropped = [c for c in code[~keep].unique() if c and c != "nan"]
    captions = [c for c in dropped if not c.upper().endswith("QY")]
    qy = [c for c in dropped if c.upper().endswith("QY")]
    return t[keep], captions, qy


def export_group_catalog(df: pd.DataFrame, out_path: Path) -> tuple[int, list[str], list[str]]:
    """Export the official group catalogue (table T0) as the runtime weight dictionary.

    Columns are positional: MDC, DRG code, name, attribute, type, 二三级 weight, 一级 weight.
    Returns (kept rows, caption cells, QY codes).
    """
    t, captions, qy = parse_group_catalog(df)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t[["mdc", "drg_code", "drg_name", "drg_attr", "drg_type", "weight_23", "weight_1"]] \
        .to_csv(out_path, index=False, encoding="utf-8-sig")
    if qy:
        (out_path.parent / "drg_qy_groups.csv").write_text(
            "mdc,qy_code\n" + "\n".join(f"{c[0]},{c}" for c in qy) + "\n", encoding="utf-8-sig")
    return len(t), captions, qy


def alias_of(name: str) -> str:
    """Stable short alias for a source file, used by downstream joins."""
    if "中医优势病种" in name:
        return "中医优势病种付费2025"
    if "细分组目录" in name:
        return "某地区细分组目录2.0"
    if "付费办法" in name:
        return "某地区DRG付费办法2025"
    if "经办管理规程" in name:
        return "某地区DRG经办规程2025"
    if "付费协议" in name:
        return "某地区DRG付费协议2025"
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", name)[:40]


def main() -> int:
    from docx import Document  # noqa: PLC0415

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    log: list[str] = [
        "# S03 官方细分组目录提取报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 来源目录：`{DOC_DIR}`",
        "",
        "## 一、文件清单与表格规模",
        "",
        "| 文件 | 段落数 | 表格数 | 提取行数 | 输出 |",
        "|---|---:|---:|---:|---|",
    ]

    for f in sorted(DOC_DIR.glob("*.docx")):
        doc = Document(str(f))
        tables = doc.tables
        all_rows: list[list[str]] = []
        for ti, t in enumerate(tables):
            for r in t.rows:
                cells = [c.text.strip().replace("\n", " ") for c in r.cells]
                all_rows.append([f"T{ti}"] + cells)
        n_col = max((len(r) for r in all_rows), default=0)
        df = pd.DataFrame([r + [""] * (n_col - len(r)) for r in all_rows]) if all_rows else pd.DataFrame()

        alias = alias_of(f.name)
        out = OUT_SUB / f"{alias}_tables.csv"
        df.to_csv(out, index=False, encoding="utf-8-sig")
        log.append(f"| {f.name[:44]} | {len(doc.paragraphs)} | {len(tables)} | {len(all_rows)} | `{out.name}` |")

        if alias == "某地区细分组目录2.0":
            n, captions, qy = export_group_catalog(df, DICT_OUT / "drg_group_catalog.csv")
            log.append("")
            log.append(f"  - **官方组目录已导出**：`src/config/dict/drg_group_catalog.csv`，共 **{n}** 组"
                       "（含二三级 / 一级两套权重，供 DRG 引擎运行时使用）")
            log.append(f"  - 剔除标题行：`{', '.join(captions)}`")
            log.append(f"  - 另有 **{len(qy)} 个 QY（未入组）伪组码**（无权重，单列到 "
                       f"`src/config/dict/drg_qy_groups.csv`）：`{', '.join(qy[:8])}` …")

        # content reconnaissance: group-code shape
        text = "\n".join(p.text for p in doc.paragraphs)
        for t in tables:
            for r in t.rows:
                text += "\n" + " | ".join(c.text for c in r.cells)
        codes = sorted(set(re.findall(r"\b([A-Z]{2}\d{2})\b", text)))
        log.append("")
        log.append(f"  - 段落首 200 字：{text.strip()[:200]!r}")
        log.append(f"  - 形如 XX99 的组码候选：{len(codes)} 个，样例 {codes[:12]}")
        log.append(f"  - 含 IU2F：{'IU2F' in text}；含 IU25：{'IU25' in text}")
        log.append("")

    md = "\n".join(log)
    (OUT_SUB / "s03_catalog_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
