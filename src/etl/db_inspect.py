"""Metadata inspection CLI for the source database.

Usage: python src/etl/db_inspect.py {tables|cols|top|values} ...
Docs: docs/15-代码模块说明与作业手册.md §四.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df, query_rows  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 60)
pd.set_option("display.max_colwidth", 40)


def cmd_tables(kw: str) -> None:
    df = query_df(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME",
        params=(f"%{kw}%",), verbose=False,
    )
    print(f"### 关键词 `{kw}` 命中 {len(df)} 张表\n")
    for t in df["TABLE_NAME"]:
        print(f"- {t}")


def cmd_cols(*tables: str) -> None:
    for t in tables:
        sql = (
            "SELECT ORDINAL_POSITION AS pos, COLUMN_NAME AS col, "
            "DATA_TYPE AS type, CHARACTER_MAXIMUM_LENGTH AS len, "
            "IS_NULLABLE AS nullable "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s ORDER BY ORDINAL_POSITION"
        )
        df = query_df(sql, params=(t.split(".")[-1],), verbose=False)
        print(f"\n### `{t}` —— {len(df)} 字段\n")
        if df.empty:
            print("（未找到，确认表名）")
            continue
        print("| # | 字段 | 类型 | 长度 | 可空 |")
        print("|---:|---|---|---:|---|")
        for _, r in df.iterrows():
            ln = "" if pd.isna(r["len"]) else int(r["len"])
            print(f"| {int(r['pos'])} | {r['col']} | {r['type']} | {ln} | {r['nullable']} |")


def cmd_top(table: str, n: int = 3) -> None:
    df = query_df(f"SELECT TOP {n} * FROM {table}", verbose=False)
    print(f"\n### `{table}` 前 {n} 行（{df.shape[1]} 列）\n")
    if df.empty:
        print("（无数据）")
        return
    for _, r in df.iterrows():
        vals = []
        for c in df.columns:
            v = r[c]
            if pd.isna(v):
                continue
            vals.append(f"{c}={str(v)[:60]}")
        print("- " + " | ".join(vals))


def cmd_values(table: str, col: str, n: int = 30) -> None:
    sql = (
        f"SELECT TOP {n} {col} AS val, COUNT(1) AS cnt "
        f"FROM {table} GROUP BY {col} ORDER BY COUNT(1) DESC"
    )
    df = query_df(sql, verbose=False)
    print(f"\n### `{table}.{col}` 值分布（Top {n}，共 {len(df)} 种）\n")
    print(df.to_string(index=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "tables":
        cmd_tables(rest[0] if rest else "")
    elif mode == "cols":
        cmd_cols(*rest)
    elif mode == "top":
        cmd_top(rest[0], int(rest[1]) if len(rest) > 1 else 3)
    elif mode == "values":
        cmd_values(rest[0], rest[1], int(rest[2]) if len(rest) > 2 else 30)
    else:
        raise SystemExit(__doc__)
