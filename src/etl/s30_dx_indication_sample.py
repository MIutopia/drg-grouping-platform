"""Sample-test whether the indication ("适应症") of other diagnoses is really entered.

The hospital reported indications are "basically fully entered" for other diagnoses.
This job first locates the indication column(s), then measures real coverage on a
random sample of discharged cases, because docs/31 treats that claim as unverified.

Read-only. Docs: docs/31-AI赋能-财务问数与政策问答场景设计.md §7.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df, query_rows, scalar  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "coding"
OUT_SUB.mkdir(parents=True, exist_ok=True)

# Name variants to look for; the exact column name is not yet confirmed.
INDICATION_PATTERNS = ("%适应症%", "%适应证%")
CANDIDATE_DBS = ("SBO", "med_record", "drg")


def _sql_cols(db: str, like: str) -> str:
    return (
        f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
        f"FROM {db}.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE COLUMN_NAME LIKE N'{like}'"
    )


def discover() -> pd.DataFrame:
    """Locate columns whose name matches an indication pattern across candidate DBs."""
    parts = []
    for db in CANDIDATE_DBS:
        for pat in INDICATION_PATTERNS:
            try:
                df = query_df(_sql_cols(db, pat), verbose=False)
            except Exception as exc:  # noqa: BLE001 - a missing DB must not abort discovery
                print(f"[warn] {db} 不可访问: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            if not df.empty:
                df.insert(0, "db", db)
                parts.append(df)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return out.drop_duplicates(subset=["db", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME"])


def list_diagnose_tables() -> pd.DataFrame:
    """List tables that likely hold diagnosis rows (name contains 诊断)."""
    parts = []
    for db in CANDIDATE_DBS:
        try:
            df = query_df(
                f"SELECT TABLE_SCHEMA, TABLE_NAME FROM {db}.INFORMATION_SCHEMA.TABLES "
                f"WHERE TABLE_TYPE='BASE TABLE' AND TABLE_NAME LIKE N'%诊断%'",
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {db} 不可访问: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if not df.empty:
            df.insert(0, "db", db)
            parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def show_columns(table: str, db: str = "SBO") -> pd.DataFrame:
    return query_df(
        f"SELECT ORDINAL_POSITION, COLUMN_NAME, DATA_TYPE FROM {db}.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_NAME = N'{table}' ORDER BY ORDINAL_POSITION",
        verbose=False,
    )


def search_columns(like: str, table_like: str = "%") -> pd.DataFrame:
    """Search column names by pattern across candidate DBs."""
    parts = []
    for db in CANDIDATE_DBS:
        try:
            df = query_df(
                f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE "
                f"FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                f"WHERE COLUMN_NAME LIKE N'{like}' AND TABLE_NAME LIKE N'{table_like}'",
                verbose=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] {db} 不可访问: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if not df.empty:
            df.insert(0, "db", db)
            parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def row_count(table: str, db: str = "SBO") -> int:
    try:
        return int(scalar(f"SELECT COUNT(1) FROM {db}.dbo.{table}") or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 计数失败 {db}.dbo.{table}: {str(exc)[:80]}")
        return -1


# 列(kind_input) 3 (discharge) and 4 (final) are near-duplicate snapshots: counting both
# double-counts every diagnosis. Pick the final version when present, else the discharge
# one, then collapse to one row per (case, 列(order)) before measuring coverage.
DX_SQL = """
WITH pick AS (
    SELECT 列(hospital_no), MAX(列(kind_input)) AS ki
    FROM 源表(diagnosis)
    WHERE 列(hospital_no) IN ({ph}) AND 列(kind_input) IN (3, 4)
    GROUP BY 列(hospital_no)
),
dx AS (
    SELECT d.列(hospital_no), d.列(order),
           MAX(CASE WHEN d.列(instatus) IS NOT NULL AND d.列(instatus) > 0 THEN 1 ELSE 0 END) AS filled
    FROM 源表(diagnosis) d
    JOIN pick p ON p.列(hospital_no) = d.列(hospital_no) AND p.ki = d.列(kind_input)
    WHERE d.列(kind_diagnose) = 2 AND d.列(order) > 1
    GROUP BY d.列(hospital_no), d.列(order)
)
SELECT 列(hospital_no), COUNT(1) AS dx_rows, SUM(filled) AS filled
FROM dx
GROUP BY 列(hospital_no)
"""


def sample_cases(n: int, since: str) -> pd.DataFrame:
    """Deterministic pseudo-random sample of discharged cases (reproducible)."""
    sql = (
        f"SELECT TOP {int(n)} 列(hospital_no), 列(time_out), 列(doctor_residency) "
        f"FROM 源表(admission) WHERE 列(time_out) >= %s AND 列(hospital_no) IS NOT NULL "
        f"ORDER BY CHECKSUM(列(hospital_no)), 列(hospital_no)"
    )
    return query_df(sql, params=(since,), verbose=False)


def sample(n: int, since: str) -> None:
    cases = sample_cases(n, since)
    if cases.empty:
        print(f"- 窗口 {since} 起无出院病例。")
        return
    nos = cases[src_col("hospital_no")].astype(str).tolist()
    ph = ",".join(["%s"] * len(nos))
    dx = query_df(DX_SQL.format(ph=ph), params=nos, verbose=False)

    m = cases.merge(dx, on=src_col("hospital_no"), how="left")
    m["dx_rows"] = m["dx_rows"].fillna(0).astype(int)
    m["filled"] = m["filled"].fillna(0).astype(int)

    tot_dx = int(m["dx_rows"].sum())
    tot_fill = int(m["filled"].sum())
    print(f"## 抽样结果（{len(m)} 例，出院时间 >= {since}）")
    print(f"- 有『其他诊断(西医,终版)』的病例：**{int((m['dx_rows'] > 0).sum())}/{len(m)}** 例")
    print(f"- 其他诊断总条数：**{tot_dx:,}** 条")
    print(f"- 其中 列(instatus) 已填（非 NULL 且 >0）：**{tot_fill:,}** 条 "
          f"（{tot_fill / tot_dx:.1%}）" if tot_dx else "- 无其他诊断，无法计算填充率")
    print()
    if (m["dx_rows"] > 0).any():
        sub = m[m["dx_rows"] > 0].copy()
        sub["rate"] = sub["filled"] / sub["dx_rows"]
        full = int((sub["rate"] >= 1.0).sum())
        zero = int((sub["rate"] <= 0.0).sum())
        print(f"- 病例级：全部填写 **{full}** 例 / 全未填 **{zero}** 例 / 部分填写 "
              f"**{len(sub) - full - zero}** 例")
        print(f"- 病例级填充率中位数：**{sub['rate'].median():.1%}**")
    print()
    print("### 明细（前 20 例）")
    print(m.head(20).to_string(index=False))

    # Cross-check: the homepage field is the one actually uploaded, so its state matters more.
    hp = query_df(
        "SELECT [入院病情] AS val, COUNT(1) AS n FROM med_record.dbo.源表(case_man) "
        "GROUP BY [入院病情] ORDER BY COUNT(1) DESC", verbose=False)

    lines = [
        "# S30 其他诊断『入院病情』抽样验证", "",
        f"- 生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M:%S}",
        f"- 样本：{len(m)} 例（出院 >= {since}，CHECKSUM 确定性抽样，可复现）",
        f"- 口径：源表(diagnosis)，列(order)>1 且 列(kind_diagnose)=2(西医)；"
        f"列(kind_input) 取终版(4)优先、否则出院(3)，并按 (病例,列(order)) 去重",
        "",
        "## 结果",
        f"- 有其他诊断的病例：**{int((m['dx_rows'] > 0).sum())}/{len(m)}** 例",
        f"- 其他诊断总条数：**{tot_dx:,}** 条",
        f"- 列(instatus) 已填：**{tot_fill:,}** 条"
        + (f"（**{tot_fill / tot_dx:.1%}**）" if tot_dx else ""),
    ]
    if (m["dx_rows"] > 0).any():
        sub = m[m["dx_rows"] > 0].copy()
        sub["rate"] = sub["filled"] / sub["dx_rows"]
        full = int((sub["rate"] >= 1.0).sum())
        lines += [
            f"- 病例级：全填 **{full}** 例 / 部分填 **{len(sub) - full}** 例 / 全空 "
            f"**{int((sub['rate'] <= 0).sum())}** 例",
        ]
    lines += ["", "## 病案首页『入院病情』交叉核对（上传件来源）", "",
              hp.to_string(index=False) if not hp.empty else "- 查询失败",
              "",
              "> 注意：明细表已判断≠首页已判断。上传医保的是首页/结算清单，"
              "两侧不一致会使 CC/MCC 归因失真。"]
    out = OUT_SUB / "s30_indication_sample.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n> 报告已写入 {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="locate the indication column and diagnosis tables")
    ap.add_argument("--db", default="SBO", help="database for --cols (default SBO)")
    ap.add_argument("--cols", metavar="TABLE", default=None,
                    help="dump the columns of TABLE")
    ap.add_argument("--pattern", metavar="LIKE", default=None,
                    help="search column names by LIKE pattern, e.g. %%症%%")
    ap.add_argument("--table-like", metavar="LIKE", default="%",
                    help="restrict --pattern to tables matching this pattern")
    ap.add_argument("--sql", metavar="SELECT", default=None,
                    help="run an ad-hoc read-only SELECT and print the result")
    ap.add_argument("--file", metavar="PATH", default=None,
                    help="run a read-only SELECT stored in a .sql file")
    ap.add_argument("--save", metavar="NAME", default=None,
                    help="also write the result to out/coding/NAME.csv")
    ap.add_argument("--groupby", metavar="COL", default=None,
                    help="print value counts of COL instead of the whole result")
    ap.add_argument("--sample", type=int, default=None, metavar="N",
                    help="randomly sample N discharged cases and measure indication coverage")
    ap.add_argument("--since", default="2026-03-01",
                    help="discharge-date lower bound for --sample (default 2026-03-01)")
    a = ap.parse_args()

    if not (a.discover or a.cols or a.pattern or a.sql or a.file or a.sample):
        ap.print_help()
        return 1

    if a.sql:
        df = query_df(a.sql, verbose=False)
    elif a.file:
        sql = Path(a.file).read_text(encoding="utf-8")
        df = query_df(sql, verbose=False)
    else:
        df = None

    if df is not None:
        print(f"- 返回 **{len(df):,}** 行")
        if a.groupby and a.groupby in df.columns:
            print(df[a.groupby].value_counts(dropna=False).to_string())
        else:
            print(df.head(30).to_string(index=False))
        if a.save:
            out = OUT_SUB / f"{a.save}.csv"
            df.to_csv(out, index=False, encoding="utf-8-sig")
            print(f"\n> 明细已写入 {out}")
        return 0

    if a.sample:
        sample(a.sample, a.since)
        return 0

    if a.discover:
        print("## 一、『适应症』字段发现")
        df = discover()
        if df.empty:
            print("- 未在 SBO / med_record / drg 中发现列名含『适应症』『适应证』的字段。")
        else:
            print(df.to_string(index=False))
        print()
        print("## 二、诊断相关表清单")
        t = list_diagnose_tables()
        print(t.to_string(index=False) if not t.empty else "- 无")

    if a.pattern:
        print(f"\n## 列名匹配 {a.pattern}（表限定 {a.table_like}）")
        df = search_columns(a.pattern, a.table_like)
        print(df.to_string(index=False) if not df.empty else "- 无匹配")

    if a.cols:
        parts = a.cols.split(".")
        tbl = parts[-1]
        db = parts[0] if len(parts) > 1 else a.db
        print(f"\n## {db}.dbo.{tbl} 字段清单（共 {row_count(tbl, db):,} 行）")
        print(show_columns(tbl, db).to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
