"""Inpatient course-record and physician-order probe.

Pulls, for a deterministic case sample, the narrative and order sources the hospital
pointed at: admission record (现病史/既往史), 首次病程记录, plus long-term and
temporary physician orders. Read-only.

Reuses s30's sampler so every probe works on the very same case list.
Docs: docs/31-AI赋能-财务问数与政策问答场景设计.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402
from s30_dx_indication_sample import sample_cases  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "course"
OUT_SUB.mkdir(parents=True, exist_ok=True)

FIRST_COURSE = "首次病程记录"


def _in(n: int) -> str:
    return ",".join(["%s"] * n)


def _blank_len(col: str) -> str:
    return f"LEN(LTRIM(RTRIM(ISNULL([{col}], ''))))"


def pull(n: int, since: str) -> pd.DataFrame:
    cases = sample_cases(n, since)
    nos = cases[src_col("hospital_no")].astype(str).tolist()
    ph = _in(len(nos))

    adm = query_df(
        "SELECT [住院号], "
        f"{_blank_len('现病史')} AS len_现病史, "
        f"{_blank_len('既往史')} AS len_既往史, "
        f"{_blank_len('西医其他诊断')} AS len_其他诊断, "
        f"{_blank_len('西医其他诊断_疾病编码')} AS len_其他诊断编码 "
        "FROM med_record.dbo.源表(med_record) "
        f"WHERE [住院号] IN ({ph})", params=nos, verbose=False)

    course = query_df(
        "SELECT [住院号], COUNT(1) AS n_首次病程, "
        f"MAX({_blank_len('病案内容')}) AS len_首次病程 "
        "FROM med_record.dbo.hospital_course_record "
        f"WHERE [病案名称] = N'{FIRST_COURSE}' AND [住院号] IN ({ph}) "
        "GROUP BY [住院号]", params=nos, verbose=False)

    # 列(cost_note) holds the item name; 列(cost_item)/列(cost_kind) are only categories.
    lng = query_df(
        "SELECT 列(hospital_no), COUNT(1) AS n_长期医嘱, "
        "COUNT(DISTINCT 列(cost_note)) AS 品目数_长期 "
        "FROM 源表(advice_long) "
        f"WHERE 列(hospital_no) IN ({ph}) GROUP BY 列(hospital_no)",
        params=nos, verbose=False)

    tmp = query_df(
        "SELECT 列(hospital_no), COUNT(1) AS n_临时医嘱, "
        "COUNT(DISTINCT 列(cost_note)) AS 品目数_临时 "
        "FROM 源表(advice_temp) "
        f"WHERE 列(hospital_no) IN ({ph}) GROUP BY 列(hospital_no)",
        params=nos, verbose=False)

    adm = adm.rename(columns={"住院号": src_col("hospital_no")})
    course = course.rename(columns={"住院号": src_col("hospital_no")})
    m = cases
    for d in (adm, course, lng, tmp):
        m = m.merge(d, on=src_col("hospital_no"), how="left")
    # presence must be captured before the numeric fillna below
    m["has_adm"] = m["len_现病史"].notna()

    num = ["len_现病史", "len_既往史", "len_其他诊断", "len_其他诊断编码",
           "n_首次病程", "len_首次病程", "n_长期医嘱", "品目数_长期",
           "n_临时医嘱", "品目数_临时"]
    for c in num:
        if c in m.columns:
            m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0).astype(int)
    return m


def report(m: pd.DataFrame, since: str) -> None:
    total = len(m)
    print(f"## 病程/医嘱取数（{total} 例，出院 >= {since}）\n")

    def rate(col: str, cmp_col: str | None = None) -> str:
        if cmp_col is None:
            hit = int((m[col] > 0).sum()) if col in m.columns else 0
        else:
            hit = int((m[col].notna()).sum())
        return f"{hit}/{total}（{hit / total:.1%}）"

    rows = [
        ("入院记录存在（源表(med_record)）",
         f"{int(m['has_adm'].sum())}/{total}"
         f"（{m['has_adm'].sum() / total:.1%}）"),
        ("　└ 现病史 非空", rate("len_现病史")),
        ("　└ 既往史 非空", rate("len_既往史")),
        ("　└ 西医其他诊断 非空", rate("len_其他诊断")),
        ("　└ 西医其他诊断_编码 非空", rate("len_其他诊断编码")),
        ("首次病程记录存在（hospital_course_record）", rate("n_首次病程")),
        ("长期医嘱 有记录", rate("n_长期医嘱")),
        ("临时医嘱 有记录", rate("n_临时医嘱")),
    ]
    print("| 来源 | 覆盖 |")
    print("|---|---|")
    for k, v in rows:
        print(f"| {k} | {v} |")

    print("\n### 文本长度（非空例的中位数，字）")
    for c in ("len_现病史", "len_既往史", "len_首次病程"):
        sub = m.loc[m[c] > 0, c]
        print(f"- {c}：{int(sub.median()) if len(sub) else 0}（最大 {int(sub.max()) if len(sub) else 0}）")

    print("\n### 明细（前 15 例）")
    cols = [c for c in (src_col("hospital_no"), src_col("doctor_residency"), "len_现病史", "len_既往史",
                        "n_首次病程", "len_首次病程", "n_长期医嘱", "n_临时医嘱") if c in m.columns]
    print(m[cols].head(15).to_string(index=False))


def dump_one(no: str) -> None:
    row = query_df(
        "SELECT TOP 1 [病案名称], [病案内容], [记录医生], [书写时间] "
        "FROM med_record.dbo.hospital_course_record "
        f"WHERE [住院号] = N'{no}' AND [病案名称] = N'{FIRST_COURSE}' "
        "ORDER BY [书写时间]", verbose=False)
    if row.empty:
        print(f"- 住院号 {no} 无首次病程记录")
    else:
        r = row.iloc[0]
        print(f"### {no} 首次病程记录（{r['记录医生']} / {r['书写时间']}）\n")
        print(str(r["病案内容"])[:1200])

    for tbl, label in (("源表(advice_long)", "长期医嘱"),
                       ("源表(advice_temp)", "临时医嘱")):
        items = query_df(
            "SELECT TOP 12 列(cost_note) AS 项目, COUNT(1) AS 次数 "
            f"FROM {tbl} WHERE 列(hospital_no) = N'{no}' "
            "GROUP BY 列(cost_item) ORDER BY COUNT(1) DESC", verbose=False)
        print(f"\n#### {label} top12\n")
        print(items.to_string(index=False) if not items.empty else "- 无记录")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", type=int, default=None, metavar="N",
                    help="pull course/order coverage for N sampled cases")
    ap.add_argument("--since", default="2026-03-01")
    ap.add_argument("--dump", default=None, metavar="住院号",
                    help="print the 首次病程记录 text and top order items")
    ap.add_argument("--dist", action="store_true",
                    help="print the 病案名称 distribution")
    a = ap.parse_args()

    if a.dist:
        d = query_df(
            "SELECT TOP 30 [病案名称] AS 病案名称, COUNT(1) AS n "
            "FROM med_record.dbo.hospital_course_record "
            "GROUP BY [病案名称] ORDER BY COUNT(1) DESC", verbose=False)
        print(d.to_string(index=False))
        return 0

    if a.dump:
        dump_one(a.dump)
        return 0

    if a.pull:
        m = pull(a.pull, a.since)
        report(m, a.since)
        out = OUT_SUB / "s31_course_coverage.csv"
        m.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n> 明细已写入 {out}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
