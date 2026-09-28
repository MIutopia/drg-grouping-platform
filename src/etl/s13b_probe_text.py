"""S13b Locate machine-readable clinical text (records) across the whole SQL Server.

The S13 probe showed the in-house 病历 table is abandoned (24 rows, last written 2018) and
the prescription table has 7 rows, so narrative records are NOT in the SBO database. This
script answers two follow-up questions before ruling anything out:
  1. which databases exist on the same instance (the EMR may simply be another database);
  2. which columns across the instance look like long free text (candidate record content).
It also samples the live order table to see what the medication data actually contains.
Docs: docs/20-AI辅助编码可行性评估.md.
"""

from __future__ import annotations

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

# Free-text column types worth looking at (SQL Server type names in sys.types).
TEXT_TYPES = ("varchar", "nvarchar", "text", "ntext")


def databases() -> list[str]:
    log = ["## 一、实例上的数据库", ""]
    d = query_df("SELECT name, database_id, create_date FROM sys.databases ORDER BY database_id")
    log += [d.to_markdown(index=False) if _md_ok(d) else "```\n" + d.to_string(index=False) + "\n```",
            "",
            "> 病历文书若不在当前 SBO 库，很可能在独立的电子病历（EMR）库或第三方系统，"
            "需信息科确认实例与账号范围。", ""]
    print("\n".join(log))
    return log


def text_columns(min_len: int = 2000) -> list[str]:
    """Columns long enough to hold narrative text, across every accessible database."""
    log = ["## 二、全实例「长文本」字段扫描", "",
           f"- 判据：字符串类型且 `max_length` ≥ {min_len} 字符", ""]
    sql = (
        "SELECT DB_NAME() AS db, t.name AS tbl, c.name AS col, ty.name AS typ, "
        "c.max_length AS max_len, p.rows AS rows_n "
        "FROM sys.columns c "
        "JOIN sys.tables t ON t.object_id = c.object_id "
        "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
        "LEFT JOIN (SELECT object_id, SUM(rows) AS rows FROM sys.partitions "
        "           WHERE index_id IN (0,1) GROUP BY object_id) p ON p.object_id = t.object_id "
        f"WHERE ty.name IN ({','.join(chr(39) + x + chr(39) for x in TEXT_TYPES)}) "
        f"AND (c.max_length >= {min_len} OR c.max_length = -1) "
        "ORDER BY p.rows DESC")
    d = query_df(sql)
    if d.empty:
        log.append("- 未发现长文本字段。")
    else:
        log += [d.head(40).to_markdown(index=False) if _md_ok(d) else
                "```\n" + d.head(40).to_string(index=False) + "\n```", ""]
    print("\n".join(log))
    return log


def order_content() -> list[str]:
    log = ["## 三、医嘱表实际内容（用药与诊疗）", ""]
    items = query_df(
        "SELECT TOP 25 列(cost_item), 列(cost_kind), COUNT(1) AS n, "
        "SUM(列(cost_number) * 列(cost_price)) AS amount FROM 源表(advice_long) "
        f"WHERE 列(time_input) >= '{DAYS_FROM}' GROUP BY 列(cost_item), 列(cost_kind) "
        "ORDER BY COUNT(1) DESC")
    log += ["### 3.1 长期医嘱 Top 25 项目", "",
            items.to_markdown(index=False) if _md_ok(items) else
            "```\n" + items.to_string(index=False) + "\n```", ""]

    kinds = query_df(
        "SELECT 列(cost_kind), COUNT(DISTINCT 列(cost_item)) AS items, COUNT(1) AS n "
        "FROM 源表(advice_long) "
        f"WHERE 列(time_input) >= '{DAYS_FROM}' GROUP BY 列(cost_kind) ORDER BY COUNT(1) DESC")
    log += ["### 3.2 收费类别与项目数", "",
            kinds.to_markdown(index=False) if _md_ok(kinds) else
            "```\n" + kinds.to_string(index=False) + "\n```", ""]

    memo = query_df(
        "SELECT TOP 12 列(cost_item), 列(cost_memo), 列(cost_note), 列(dose_number), 列(dose_unit), "
        "列(dose_use) FROM 源表(advice_long) "
        f"WHERE 列(time_input) >= '{DAYS_FROM}' AND 列(cost_memo) IS NOT NULL")
    log += ["### 3.3 医嘱备注/用法样例（判断是否含临床语义）", "",
            memo.to_markdown(index=False) if _md_ok(memo) else
            "```\n" + memo.to_string(index=False) + "\n```", ""]
    print("\n".join(log))
    return log


def _md_ok(df: pd.DataFrame) -> bool:
    try:
        df.to_markdown(index=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def probe_db(name: str) -> int:
    """Inventory another database on the same instance: tables and long-text columns."""
    log = [f"# 数据库 `{name}` 探测", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    try:
        n = query_df(f"SELECT COUNT(1) AS n FROM [{name}].sys.tables").iloc[0, 0]
        log.append(f"- 表数量：**{int(n):,}**")
    except Exception as e:  # noqa: BLE001
        log += [f"- **当前账号无法访问**：`{str(e)[:220]}`", "",
                "> 需信息科开通该库的只读权限后方可继续评估。"]
        md = "\n".join(log)
        (OUT_SUB / f"s13b_db_{name}.md").write_text(md, encoding="utf-8")
        print(md)
        return 1

    top = query_df(
        f"SELECT TOP 30 t.name AS tbl, SUM(p.rows) AS rows_n "
        f"FROM [{name}].sys.tables t "
        f"JOIN [{name}].sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1) "
        "GROUP BY t.name ORDER BY SUM(p.rows) DESC")
    log += ["", "## 一、表清单（按行数 Top 30）", "", _md(top), ""]

    cols = query_df(
        "SELECT TOP 60 t.name AS tbl, c.name AS col, ty.name AS typ, c.max_length AS max_len, "
        f"SUM(p.rows) AS rows_n FROM [{name}].sys.columns c "
        f"JOIN [{name}].sys.tables t ON t.object_id = c.object_id "
        f"JOIN [{name}].sys.types ty ON ty.user_type_id = c.user_type_id "
        f"JOIN [{name}].sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1) "
        "WHERE ty.name IN ('varchar','nvarchar','text','ntext') "
        "AND (c.max_length >= 500 OR c.max_length = -1) "
        "GROUP BY t.name, c.name, ty.name, c.max_length ORDER BY SUM(p.rows) DESC")
    log += ["## 二、长文本字段（≥500 字符，**病历正文候选**）", "",
            _md(cols) if len(cols) else "- 无", ""]
    md = "\n".join(log)
    (OUT_SUB / f"s13b_db_{name}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) if _md_ok(df) else "```\n" + df.to_string(index=False) + "\n```"


def _try(sql: str) -> pd.DataFrame | None:
    """Run a probe query, returning None instead of aborting the whole report."""
    try:
        return query_df(sql)
    except Exception as e:  # noqa: BLE001
        print(f"    [probe] 查询失败：{str(e)[:90]}")
        return None


def probe_table(spec: str) -> int:
    """Dump columns and fill rates for db.table, focusing on coding-relevant fields."""
    db, tbl = spec.split(".", 1)
    log = [f"# 表 `{db}.{tbl}` 详情", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    cols = query_df(
        "SELECT c.name AS col, ty.name AS typ, c.max_length AS max_len, "
        "c.is_nullable AS nullable "
        f"FROM [{db}].sys.columns c JOIN [{db}].sys.tables t ON t.object_id = c.object_id "
        f"JOIN [{db}].sys.types ty ON ty.user_type_id = c.user_type_id "
        f"WHERE t.name = '{tbl}' ORDER BY c.column_id")
    if cols.empty:
        print(f"表 {spec} 不存在"); return 1
    total = query_df(f"SELECT COUNT(1) AS n FROM [{db}].[dbo].[{tbl}]").iloc[0, 0]
    log += [f"- 行数：**{int(total):,}**，字段数：**{len(cols)}**", "",
            "## 一、字段清单", "", _md(cols), ""]

    # fill rate of every text column: tells us which fields carry real content
    rows = []
    for col in cols["col"]:
        r = _try(f"SELECT SUM(CASE WHEN [{col}] IS NOT NULL AND LTRIM(RTRIM(CAST([{col}] AS "
                 f"VARCHAR(MAX)))) <> '' THEN 1 ELSE 0 END) AS filled, "
                 f"AVG(LEN(CAST([{col}] AS VARCHAR(MAX)))) AS avg_len "
                 f"FROM [{db}].[dbo].[{tbl}]")
        if r is None:
            continue
        filled = int(r.iloc[0]["filled"] or 0)
        avg_len = r.iloc[0]["avg_len"]
        rows.append({"col": col, "filled": filled,
                     "fill_rate": f"{filled / int(total):.1%}" if total else "—",
                     "avg_len": round(float(avg_len), 1) if pd.notna(avg_len) else 0})
    fr = pd.DataFrame(rows).sort_values("filled", ascending=False)
    log += ["## 二、字段填充率（**判定哪些字段真正可用**）", "", _md(fr), ""]
    md = "\n".join(log)
    (OUT_SUB / f"s13b_tbl_{tbl}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def main() -> int:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="探测同实例下的指定数据库")
    ap.add_argument("--table", default=None, metavar="DB.TABLE", help="探测指定表的字段与填充率")
    a = ap.parse_args()
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    if a.table:
        return probe_table(a.table)
    if a.db:
        return probe_db(a.db)

    log = ["# S13b 全实例临床文本定位", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    for fn in (databases, text_columns, order_content):
        try:
            log += fn()
        except Exception as e:  # noqa: BLE001
            log.append(f"- `{fn.__name__}` 失败：{str(e)[:120]}")
            log.append("")
    (OUT_SUB / "s13b_text_probe.md").write_text("\n".join(log), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
