"""S01 Import monthly medicare settlement returns with schema adaptation.

Reads 原始资料/DRG结算样表/*.xlsx (three schemas observed so far) and writes a single
normalised table plus an import report.
Docs: docs/15-代码模块说明与作业手册.md §6.2.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR, RAW_DIR  # noqa: E402

SETTLE_DIR = RAW_DIR / "DRG结算样表"
OUT_SUB = OUT_DIR / "settlement"
HEADER_SCAN_ROWS = 12

_CLEAN_RE = re.compile(r"[\s\u3000]+")


# ------------------------------------------------------------------
# Schema mapping
# ------------------------------------------------------------------
def load_schema() -> dict:
    return json.loads((CONFIG_DIR / "settlement_schema.json").read_text(encoding="utf-8"))


def _norm(s: object) -> str:
    return _CLEAN_RE.sub("", str(s or ""))


def build_column_map(header: list[str], schema: dict) -> tuple[dict[str, int], list[str]]:
    """Return {canonical field: column index} and the list of unresolved fields."""
    idx_norm = {_norm(h): i for i, h in enumerate(header)}
    mapping: dict[str, int] = {}

    for field, spec in schema["fields"].items():
        for cand in spec["candidates"]:
            key = _norm(cand)
            if key in idx_norm:                       # exact match after whitespace strip
                mapping[field] = idx_norm[key]
                break
        else:                                          # fall back to substring match
            for cand in spec["candidates"]:
                key = _norm(cand)
                hit = next((i for k, i in idx_norm.items() if key and key in k), None)
                if hit is not None:
                    mapping[field] = hit
                    break

    unmatched = [f for f in schema["fields"] if f not in mapping]
    return mapping, unmatched


def guess_header_row(raw: pd.DataFrame) -> int:
    keywords = ("结算", "住院", "病案", "病组", "诊断", "编码", "费用", "医保", "权",
                "支付", "患者", "姓名", "金额", "科室", "入组", "清单", "序号")
    best, best_score = 0, -1
    for r in range(min(HEADER_SCAN_ROWS, raw.shape[0])):
        row = raw.iloc[r]
        text = "".join(str(v) for v in row.tolist())
        score = sum(10 for k in keywords if k in text) + int(row.notna().sum())
        if score > best_score:
            best, best_score = r, score
    return best


# ------------------------------------------------------------------
# Value normalisation
# ------------------------------------------------------------------
def _is_na(v: object) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def to_num(v: object) -> float | None:
    if _is_na(v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("￥", "").replace("元", "")
    if s in ("", "-", "nan", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_pct(v: object) -> float | None:
    """'100.00%' -> 100.0; numeric input is returned as-is."""
    n = to_num(v)
    return n


def to_flag01(v: object, vmap: dict) -> int | None:
    if _is_na(v):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s in vmap:
        return int(vmap[s])
    n = to_num(s)
    return int(n) if n is not None else None


# Plausible window for parsed dates. The source sheets carry far-future junk (a 2250 date was
# observed) which pandas accepts as a legal Timestamp; it then produced absurd length-of-stay
# values ("80,000 days") that flowed into the drg database (code review Q3). Out-of-range
# values become None and are listed in the import report so the source can be corrected.
_MIN_YEAR, _MAX_YEAR = 2000, 2035
OUT_OF_RANGE_DATES: list[str] = []


def _parse_date(v: object) -> pd.Timestamp | None:
    """Parse one value into a Timestamp (no range check)."""
    if _is_na(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v
    if isinstance(v, datetime):
        return pd.Timestamp(v)
    s = str(v).strip()
    if not s:
        return None

    utc_marked = s.endswith("+")
    if utc_marked:
        s = s[:-1]
    try:
        ts = pd.to_datetime(s)
    except Exception:  # noqa: BLE001
        return None
    if utc_marked and ts.hour != 0:
        ts = ts + timedelta(hours=8)
    return ts


def to_date(v: object) -> pd.Timestamp | None:
    """Parse a timestamp column, rejecting values outside a plausible window.

    The 2026-03 sheet serialises local midnight as UTC ('2026-02-20T16:00:00.000+'),
    which would shift every date back by 8 hours (admission lands on the previous day).
    Values ending with '+' are therefore parsed as UTC and moved forward by 8 hours;
    cross-checked against the 住院天数 column in the import report.
    """
    ts = _parse_date(v)
    if ts is None:
        return None
    if not (_MIN_YEAR <= ts.year <= _MAX_YEAR):
        if len(OUT_OF_RANGE_DATES) < 50:
            OUT_OF_RANGE_DATES.append(f"{str(v)[:40]}（{ts.year} 年）")
        return None
    return ts


def split_code_name(v: object) -> tuple[str | None, str | None]:
    """'M51.202:腰椎间盘突出' -> ('M51.202', '腰椎间盘突出')."""
    if _is_na(v):
        return None, None
    s = str(v).strip()
    if not s:
        return None, None
    if ":" in s:
        code, name = s.split(":", 1)
        return code.strip() or None, name.strip() or None
    return s, None


def split_code_name_list(v: object) -> tuple[str | None, str | None, int]:
    """'A:B | C:D' -> ('A|C', 'B|D', item_count)."""
    if _is_na(v):
        return None, None, 0
    items = [x.strip() for x in str(v).split("|") if x.strip()]
    codes, names = [], []
    for it in items:
        c, n = split_code_name(it)
        if c:
            codes.append(c)
        if n:
            names.append(n)
    return ("|".join(codes) or None), ("|".join(names) or None), len(items)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main() -> int:
    schema = load_schema()
    fields = schema["fields"]
    vmap = schema["value_maps"]["refund_flag"]

    files = sorted(SETTLE_DIR.glob("*.xlsx"))
    if not files:
        print(f"[错误] 未找到结算返回 Excel：{SETTLE_DIR}")
        return 1

    frames: list[pd.DataFrame] = []
    report: list[str] = [
        "# S01 结算返回清单导入报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 输入目录：`{SETTLE_DIR}`",
        f"- 映射配置：`src/config/settlement_schema.json` v{schema['version']}",
        "",
        "## 一、逐 Sheet 解析结果",
        "",
        "| 文件 | Sheet | 数据行 | 列数 | 表头行 | 命中规范字段 |",
        "|---|---|---:|---:|---:|---:|",
    ]

    for f in files:
        xls = pd.ExcelFile(f, engine="openpyxl")
        for sheet in xls.sheet_names:
            raw = pd.read_excel(f, sheet_name=sheet, header=None, engine="openpyxl", dtype=object)
            if raw.dropna(how="all").empty:
                continue
            hrow = guess_header_row(raw)
            header = [str(v).strip() if pd.notna(v) else f"__unnamed_{i}__"
                      for i, v in enumerate(raw.iloc[hrow].tolist())]
            body = raw.iloc[hrow + 1:].reset_index(drop=True)
            body = body.dropna(how="all")

            cmap, unmatched = build_column_map(header, schema)
            out = pd.DataFrame(index=body.index)
            for field, col in cmap.items():
                out[field] = body.iloc[:, col]

            out["source_file"] = f.name
            out["source_sheet"] = sheet
            frames.append(out)

            report.append(
                f"| {f.name} | {sheet} | {len(body)} | {len(header)} | {hrow + 1} | {len(cmap)}/{len(fields)} |"
            )

    df = pd.concat(frames, ignore_index=True)

    # ---- normalise values -------------------------------------------------
    for field, spec in fields.items():
        if field not in df.columns:
            continue
        dt = spec["dtype"]
        if dt == "num":
            df[field] = df[field].map(to_num)
        elif dt == "num_pct":
            df[field] = df[field].map(to_pct)
        elif dt == "flag01":
            df[field] = df[field].map(lambda v: to_flag01(v, vmap))
        elif dt == "date":
            df[field] = df[field].map(to_date)
        elif dt == "code_name":
            pairs = df[field].map(split_code_name)
            df[field + "_code"] = [p[0] for p in pairs]
            df[field + "_name"] = [p[1] for p in pairs]
        elif dt == "code_name_list":
            triples = df[field].map(split_code_name_list)
            df[field + "_codes"] = [t[0] for t in triples]
            df[field + "_names"] = [t[1] for t in triples]
            df[field + "_cnt"] = [t[2] for t in triples]
        else:
            df[field] = df[field].map(lambda v: None if _is_na(v) else str(v).strip())

    # ---- ensure key columns exist -----------------------------------------
    if "main_diag_code" not in df.columns and "main_diag" in df.columns:
        df["main_diag_code"] = None
    if "main_op_code" not in df.columns and "main_op" in df.columns:
        df["main_op_code"] = None

    # ---- period and row identity ------------------------------------------
    if "settle_date" in df.columns:
        df["settle_ym"] = pd.to_datetime(df["settle_date"], errors="coerce").dt.strftime("%Y-%m")
    df["row_uid"] = df["source_file"] + "#" + df["source_sheet"] + "#" + df.index.astype(str)

    # ---- persist -----------------------------------------------------------
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_SUB / "settlement_returns.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    parquet_note = ""
    try:
        df.to_parquet(OUT_SUB / "settlement_returns.parquet", index=False)
        parquet_note = "settlement_returns.parquet"
    except Exception as exc:  # noqa: BLE001
        parquet_note = f"（parquet 未生成：{type(exc).__name__}，需 pyarrow）"

    # ---- field coverage matrix --------------------------------------------
    report += ["", "## 二、规范字段覆盖率（● 有值 / ○ 列在但全空 / - 无此列）", ""]
    per_sheet = df.groupby(["source_file", "source_sheet"], sort=False)
    present_fields = [f for f in fields if f in df.columns]
    keys = list(per_sheet.groups.keys())
    report.append("| 规范字段 | " + " | ".join(f"{k[1]}" for k in keys) + " |")
    report.append("|---" * (len(keys) + 1) + "|")
    for fld in present_fields:
        cells = []
        for k in keys:
            sub = per_sheet.get_group(k)[fld]
            cells.append("●" if sub.notna().any() else "○")
        report.append(f"| {fld} ({fields[fld]['cn']}) | " + " | ".join(cells) + " |")

    # ---- data quality checks ----------------------------------------------
    report += ["", "## 三、数据质量校验", ""]
    checks: list[tuple[str, str]] = []

    if {"weight", "fee_rate", "drg_standard"} <= set(df.columns):
        tmp = df.dropna(subset=["weight", "fee_rate", "drg_standard"]).copy()
        tmp = tmp[tmp["weight"] > 0]
        tmp["implied"] = tmp["drg_standard"] / (tmp["weight"] * tmp["fee_rate"])
        if len(tmp):
            r = tmp["implied"].describe()
            checks.append((
                "机构系数反推  支付标准 ÷ (权重 × 费率)",
                f"n={len(tmp):,}  中位数={r['50%']:.4f}  最小={r['min']:.4f}  最大={r['max']:.4f}  "
                f"（≈0.92 可解释为二级机构系数）",
            ))

    if {"in_time", "out_time", "actual_days"} <= set(df.columns):
        tmp = df.dropna(subset=["in_time", "out_time", "actual_days"]).copy()
        # 时间列经 map(to_date) 后是 object dtype（Timestamp 与 None 混杂），两列相减
        # 结果仍是 object，.dt 会抛 "Can only use .dt accessor with datetimelike values"。
        # 必须先统一转成 datetime64 再做差。
        for c in ("in_time", "out_time"):
            tmp[c] = pd.to_datetime(tmp[c], errors="coerce")
        tmp = tmp.dropna(subset=["in_time", "out_time"])
        if len(tmp):
            tmp["calc_days"] = (tmp["out_time"] - tmp["in_time"]).dt.days
            match = (tmp["calc_days"] == tmp["actual_days"]).mean()
            checks.append((
                "时间列还原校验  (出院−入院) 天数 == '住院天数' 列",
                f"n={len(tmp):,}  一致率={match:.1%}（验证时间串 +8h 还原规则是否正确）",
            ))

    if "refund_flag" in df.columns:
        vc = df["refund_flag"].value_counts(dropna=False).to_dict()
        checks.append(("退费结算标志分布", str(vc)))

    if "drg_code" in df.columns:
        top = df["drg_code"].value_counts().head(8).to_dict()
        checks.append(("DRG 组编码 Top8", str(top)))

    if OUT_OF_RANGE_DATES:
        checks.append((
            "异常日期（年份越界，已置空隔离）",
            f"{len(OUT_OF_RANGE_DATES)} 例，样例：" + "; ".join(OUT_OF_RANGE_DATES[:5]),
        ))

    for name, val in checks:
        report.append(f"- **{name}**：{val}")

    report += [
        "",
        "## 四、产出",
        "",
        f"- `{csv_path.relative_to(OUT_DIR.parent.parent)}` —— {len(df):,} 行 × {df.shape[1]} 列",
        f"- {parquet_note}",
        "",
        "## 五、待跟进",
        "",
        "- 4/5 月返回表**不含**主要/其它诊断、手术、住院天数、科室字段（列存在但全空）→ 组码分歧只能对比 `drg_code`，"
        "诊断口径分歧需回落到院内预分组与 HIS 端诊断比对；建议向医保办索取带诊断明细的清单导出。",
        "- 结算 ID（`settle_id`，如 301449785251）是官方主键，需与 HIS 侧 `列(settle_no)` / `源表(settle_bill)` 建立关联。",
    ]

    md = "\n".join(report)
    (OUT_SUB / "s01_import_report.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[完成] 统一表 {len(df):,} 行 → {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
