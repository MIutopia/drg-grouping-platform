"""Excel structure probe for schema-adaptive settlement returns.

Docs: docs/15-代码模块说明与作业手册.md §五.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HEADER_SCAN_ROWS = 12          # leading rows scanned for the real header row
SHEET_PREVIEW_ROWS = 3         # sample values printed per column


def probe(path: Path) -> None:
    print(f"# Excel 结构探测\n\n- 文件：`{path.name}`\n- 大小：{path.stat().st_size / 1024:,.0f} KB\n")

    xls = pd.ExcelFile(path, engine="openpyxl")
    print(f"- Sheet 数：{len(xls.sheet_names)}\n")

    for name in xls.sheet_names:
        raw = pd.read_excel(path, sheet_name=name, header=None, engine="openpyxl", dtype=object)
        print(f"\n## Sheet `{name}`")
        print(f"- 原始规模：{raw.shape[0]} 行 × {raw.shape[1]} 列")

        hrow = _guess_header_row(raw)
        print(f"- 推定表头行：第 {hrow + 1} 行（0-based {hrow}）")

        header = [str(v).strip() if pd.notna(v) else f"<未命名{i}>"
                  for i, v in enumerate(raw.iloc[hrow].tolist())]
        print(f"- 列数：{len(header)}")
        print()

        body = raw.iloc[hrow + 1:].reset_index(drop=True)
        print("| # | 列名 | 非空数 | 样例值 |")
        print("|---:|---|---:|---|")
        for i, col in enumerate(header):
            if i >= body.shape[1]:
                break
            series = body.iloc[:, i]
            nn = int(series.notna().sum())
            samples = [str(v).strip() for v in series.dropna().head(SHEET_PREVIEW_ROWS)]
            sample = " / ".join(s[:24] for s in samples) or "-"
            print(f"| {i + 1} | {col} | {nn} | {sample[:120]} |")

        # raw values below the header, to spot unit / total rows
        print()
        print("<details><summary>表头下前 2 行原始值</summary>\n")
        for r in range(hrow + 1, min(hrow + 3, raw.shape[0])):
            vals = [str(v)[:20] for v in raw.iloc[r].tolist()]
            print(f"- 行{r + 1}: {vals}")
        print("\n</details>")


def _guess_header_row(raw: pd.DataFrame) -> int:
    """Header row = the leading row matching the most known business keywords."""
    keywords = ("结算", "住院", "病案", "病组", "诊断", "编码", "费用", "医保", "权",
                "支付", "患者", "姓名", "金额", "科室", "入组", "清单", "序号")
    best, best_score = 0, -1
    for r in range(min(HEADER_SCAN_ROWS, raw.shape[0])):
        row = raw.iloc[r]
        nonnull = int(row.notna().sum())
        text = "".join(str(v) for v in row.tolist())
        kw_hits = sum(1 for k in keywords if k in text)
        score = kw_hits * 10 + nonnull
        if score > best_score:
            best, best_score = r, score
    return best


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("用法: python excel_probe.py <xlsx路径> [xlsx路径...]")
    for p in sys.argv[1:]:
        probe(Path(p))
        print("\n" + "=" * 70 + "\n")
