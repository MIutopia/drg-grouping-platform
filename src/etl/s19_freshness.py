"""S19 Source freshness monitoring and alerting.

docs/08 §九 requires a "backup-database freshness" alert, and docs/22 showed why the obvious
implementation is wrong: med_record.SyncControl reported the disease-case homepage as last
synced 2026-08-19 while its newest row was 2026-09-19, so trusting the control table would
have raised a false "stalled 32 days" alert.

Therefore the primary signal is always the DATA itself - MAX(business timestamp) - and
SyncControl is recorded alongside purely for comparison. Results go to drg.ops_freshness and
threshold breaches to drg.ops_alert.

Also reports the "discharged but not pre-grouped" rate required by the same section, and
watches the s20 monthly cadence - s20 is triggered manually from the web console, and a
manual cadence goes stale silently unless something raises the alarm.
Docs: docs/24-运维监控与新鲜度告警.md.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "ops"
DRG = "drg"

# (db, table, label, preferred time columns, max lag hours, group)
SOURCES = [
    ("SBO", "源表(admission)", "HIS 入院登记", (src_col("time_enter"),), 24, "HIS"),
    ("SBO", "源表(admission)", "HIS 出院办理", (src_col("time_out"),), 24, "HIS"),
    ("SBO", "源表(diagnosis)", "住院诊断明细", (src_col("time_input"),), 24, "HIS"),
    ("SBO", "源表(operation)", "手术操作明细", (src_col("time_input"),), 24, "HIS"),
    ("SBO", "源表(fee_cure_in)", "治疗费明细", (src_col("time_input"),), 24, "费用"),
    ("SBO", "源表(fee_check_in)", "检查费明细", (src_col("time_input"),), 24, "费用"),
    ("SBO", "源表(fee_medicine_in)", "药品费明细", (src_col("time_input"),), 24, "费用"),
    ("med_record", "源表(med_record)", "住院病历文书", ("录入时间", "入院时间"), 24,
     "备份库"),
    ("med_record", "源表(case_man)", "病案首页", ("录入时间", "入院时间"), 48, "备份库"),
    ("records", "out_medical_records", "门诊病历", None, 24, "备份库"),
]

# Files whose arrival time matters. Settlement returns are monthly, hence the wide window.
FILES = [
    (OUT_DIR / "settlement" / "settlement_returns.csv", "医保结算返回导入", 45 * 24),
]

STATUS_OK, STATUS_WARN, STATUS_ALERT, STATUS_UNKNOWN = "ok", "warn", "alert", "unknown"


def _date_columns(db: str, table: str) -> list[str]:
    d = query_df(
        "SELECT COLUMN_NAME FROM " + db + ".INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_NAME = '{table}' AND DATA_TYPE LIKE '%date%' "
        "ORDER BY ORDINAL_POSITION")
    return d["COLUMN_NAME"].tolist() if len(d) else []


def _safe_max(db: str, table: str, col: str):
    """MAX(col) restricted to a plausible range.

    Two data traps were found on the first run: 源表(operation).列(time) holds 2250-07-16 and
    records.out_medical_records.出生年月 holds 7508-08-03. Bounding the window above by
    GETDATE() removes that noise, and preferring the largest value across candidate columns
    keeps a birth-date column from being mistaken for the business timestamp.
    """
    try:
        r = query_df(f"SELECT MAX([{col}]) AS ts FROM {db}.dbo.{table} "
                     f"WHERE [{col}] >= '2000-01-01' AND [{col}] <= GETDATE()").iloc[0]
        return pd.to_datetime(r["ts"], errors="coerce")
    except Exception:  # noqa: BLE001
        return pd.NaT


def _pick_business_ts(db: str, table: str, preferred: tuple[str, ...] | None):
    """Return (column, timestamp): honour the explicit column, else the newest plausible one."""
    cols = _date_columns(db, table)
    if not cols:
        return None, pd.NaT
    for p in (preferred or ()):
        if p in cols:
            ts = _safe_max(db, table, p)
            if pd.notna(ts):
                return p, ts
    best_col, best_ts = None, pd.NaT
    for c in cols:
        ts = _safe_max(db, table, c)
        if pd.notna(ts) and (pd.isna(best_ts) or ts > best_ts):
            best_col, best_ts = c, ts
    return best_col, best_ts


def _sync_ctrl() -> dict[str, datetime]:
    """Read SyncControl from the backup databases, for reference only."""
    out: dict[str, datetime] = {}
    for db in ("med_record", "records"):
        try:
            d = query_df(f"SELECT TableName, LastSyncTime FROM {db}.dbo.SyncControl")
            for r in d.itertuples():
                out[f"{db}.{str(r.TableName).strip()}"] = r.LastSyncTime
        except Exception:  # noqa: BLE001  - the control table is optional
            continue
    return out


def probe() -> pd.DataFrame:
    now = datetime.now()
    sync = _sync_ctrl()
    rows = []
    for db, table, label, preferred, max_lag, group in SOURCES:
        col, ts = _pick_business_ts(db, table, preferred)
        try:
            n = query_df(
                "SELECT SUM(p.rows) AS n FROM " + db + ".sys.partitions p "
                "JOIN " + db + ".sys.tables t ON t.object_id = p.object_id "
                f"WHERE t.name = '{table}' AND p.index_id IN (0,1)").iloc[0]["n"]
            lag = (now - ts.to_pydatetime()).total_seconds() / 3600 if pd.notna(ts) else None
        except Exception as e:  # noqa: BLE001
            print(f"    [!] {db}.{table} 探测失败：{str(e)[:70]}")
            rows.append({"source_db": db, "source_table": table, "label": label, "group": group,
                         "time_col": col or "", "business_ts": None, "rows_total": None,
                         "lag_hours": None, "max_lag": max_lag, "sync_ctrl_ts": None,
                         "status": STATUS_UNKNOWN})
            continue

        status = STATUS_UNKNOWN
        if lag is not None:
            status = STATUS_OK if lag <= max_lag else (
                STATUS_WARN if lag <= max_lag * 2 else STATUS_ALERT)
        rows.append({"source_db": db, "source_table": table, "label": label, "group": group,
                     "time_col": col, "business_ts": ts, "rows_total": int(n) if pd.notna(n) else None,
                     "lag_hours": round(lag, 2) if lag is not None else None,
                     "max_lag": max_lag,
                     "sync_ctrl_ts": sync.get(f"{db}.{table}"), "status": status})

    for path, label, max_lag in FILES:
        if not path.exists():
            rows.append({"source_db": "-", "source_table": path.name, "label": label,
                         "group": "文件", "time_col": "mtime", "business_ts": None,
                         "rows_total": None, "lag_hours": None, "max_lag": max_lag,
                         "sync_ctrl_ts": None, "status": STATUS_ALERT})
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        lag = (now - mtime).total_seconds() / 3600
        rows.append({"source_db": "-", "source_table": path.name, "label": label, "group": "文件",
                     "time_col": "mtime", "business_ts": mtime,
                     "rows_total": int(path.stat().st_size), "lag_hours": round(lag, 2),
                     "max_lag": max_lag, "sync_ctrl_ts": None,
                     "status": STATUS_OK if lag <= max_lag else STATUS_ALERT})
    return pd.DataFrame(rows)


def pregroup_rate() -> pd.DataFrame:
    """Share of recent discharges with no vendor pre-grouping row (docs/08 §九 巡检项)."""
    d = query_df(
        "SELECT COUNT(1) AS discharged, "
        "SUM(CASE WHEN g.列(serial_man) IS NULL THEN 1 ELSE 0 END) AS not_grouped "
        "FROM 源表(admission) m LEFT JOIN 源表(vendor_pre_grouping) g ON g.列(serial_man) = m.列(serial) "
        "WHERE m.列(time_out) >= DATEADD(day, -30, GETDATE()) AND m.列(time_out) IS NOT NULL")
    r = d.iloc[0]
    n, bad = int(r["discharged"] or 0), int(r["not_grouped"] or 0)
    return pd.DataFrame([{"近 30 日出院例数": n, "未预分组例数": bad,
                          "未预分组率": f"{bad / n:.1%}" if n else "—",
                          "阈值": "5%", "状态": "ok" if n and bad / n <= 0.05 else
                          ("warn" if n else "unknown")}])


def regress_cadence() -> pd.DataFrame | None:
    """Monthly cadence of the s20 regression gate, which is deliberately semi-automatic.

    s20 is triggered by 信息科 from the web console once a month (docs/29 jobs allowlist);
    a manual cadence needs a reminder, so the freshness monitor also watches the job itself:
    once the last verdict is older than 35 days (1 month + grace) an open ops_alert asks
    信息科 to press the console button. Returns None when there is no run yet or the drg
    database is unreachable (fresh-install / offline tolerance, never an exception).
    """
    try:
        d = ddlio.query("SELECT TOP 1 run_at FROM ops_regress_run ORDER BY run_id DESC", DRG)
    except Exception:  # noqa: BLE001
        return None
    if d.empty:
        return None
    last = pd.to_datetime(d.iloc[0]["run_at"], errors="coerce")
    if pd.isna(last):
        return None
    days = (datetime.now() - last.to_pydatetime()).days
    return pd.DataFrame([{"最近回归运行": str(last)[:19], "距今天数": days,
                          "阈值": "≤35 天", "状态": "ok" if days <= 35 else "warn"}])


def report(fr: pd.DataFrame, pg: pd.DataFrame, rc: pd.DataFrame | None = None) -> str:
    show = fr[["group", "label", "time_col", "business_ts", "lag_hours", "max_lag",
               "sync_ctrl_ts", "status"]].copy()
    show["business_ts"] = show["business_ts"].astype(str).str[:19]
    show["sync_ctrl_ts"] = show["sync_ctrl_ts"].astype(str).str[:19]
    lines = ["# S19 数据新鲜度巡检", "",
             f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
             "- 判据：**以数据自身的业务时间戳为准**（`SyncControl` 仅作对照；"
             "实测其曾显示病案首页停滞 32 天而数据实为新鲜）", "",
             "## 一、源数据新鲜度", "", _md(show), "",
             "## 二、出院未预分组率（近 30 日）", "", _md(pg), "",
             "## 三、s20 回归月度节奏（控制台半自动）", "",
             _md(rc) if rc is not None else "- 暂无回归运行记录。", ""]
    bad = fr[fr["status"].isin((STATUS_ALERT, STATUS_WARN))]
    lines += ["## 四、需关注项", ""]
    if len(bad):
        for r in bad.itertuples():
            lines.append(f"- **[{r.status}] {r.label}**（{r.source_db}.{r.source_table}）："
                         f"最新业务时间 {str(r.business_ts)[:19]}，滞后 {r.lag_hours} 小时")
    if len(pg) and str(pg.iloc[0]["状态"]) == "warn":
        r0 = pg.iloc[0]
        lines.append(f"- **[warn] 出院未预分组率**：近 30 日 {int(r0['未预分组例数'])}/"
                     f"{int(r0['近 30 日出院例数'])} 例未预分组（{r0['未预分组率']}），"
                     f"超 {r0['阈值']} 阈值")
    if rc is not None and str(rc.iloc[0]["状态"]) == "warn":
        r0 = rc.iloc[0]
        lines.append(f"- **[warn] 回归判定超月未执行**：最近一次 {r0['最近回归运行']}"
                     f"（距今 {int(r0['距今天数'])} 天，阈值 {r0['阈值']}），"
                     "请在控制台「作业触发」中执行「回归门槛判定（s20）」")
    if (not len(bad)
            and not (len(pg) and str(pg.iloc[0]["状态"]) == "warn")
            and not (rc is not None and str(rc.iloc[0]["状态"]) == "warn")):
        lines.append("- 无。")
    lines.append("")
    return "\n".join(lines)


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def write_alerts(fr: pd.DataFrame, pg: pd.DataFrame, rc: pd.DataFrame | None = None) -> None:
    """Refresh this job's own open-alert set. Acknowledged/closed rows are preserved as history.

    Scoped by [owner]: ops_alert is shared with other jobs (S20's regression gate), so an
    unscoped "DELETE WHERE status='open'" would wipe their live alerts. Rows written before the
    owner column existed carry NULL and are still treated as this job's.
    All inserts are parameterised: Chinese written into the SQL text comes back mangled.
    """
    ddlio._exec("DELETE FROM ops_alert WHERE status = 'open' "
                "AND ([owner] = 's19' OR [owner] IS NULL)", DRG)  # noqa: SLF001
    n = 0
    for r in fr[fr["status"].isin((STATUS_ALERT, STATUS_WARN))].itertuples():
        detail = (f"{r.source_db}.{r.source_table} 最新业务时间 {str(r.business_ts)[:19]}，"
                  f"滞后 {r.lag_hours} 小时（阈值 {r.max_lag} 小时）")
        ddlio.insert_alert("high" if r.status == STATUS_ALERT else "warn",
                           r.label, detail, "s19", DRG)
        n += 1
    if len(pg) and str(pg.iloc[0]["状态"]) == "warn":
        r0 = pg.iloc[0]
        ddlio.insert_alert("warn", "出院未预分组率",
                           f"近 30 日 {int(r0['未预分组例数'])}/{int(r0['近 30 日出院例数'])} 例"
                           f"未预分组（{r0['未预分组率']}），超过 {r0['阈值']} 阈值", "s19", DRG)
        n += 1
    if rc is not None and str(rc.iloc[0]["状态"]) == "warn":
        r0 = rc.iloc[0]
        ddlio.insert_alert("warn", "回归判定超月未执行",
                           f"最近一次 s20 回归判定 {r0['最近回归运行']}（距今 "
                           f"{int(r0['距今天数'])} 天，阈值 {r0['阈值']}）；"
                           "请在控制台「作业触发」中执行「回归门槛判定（s20）」", "s19", DRG)
        n += 1
    print(f"    告警 {n} 条")


def audit_charset() -> int:
    """Find VARCHAR columns holding non-ASCII data.

    The instance collation is Chinese_PRC_CI_AS while the driver connects with charset=utf8,
    so Chinese written into a VARCHAR column comes back mangled (found via ops_alert.item)
    while NVARCHAR is unaffected. Any column that can hold Chinese must be NVARCHAR.
    """
    cols = ddlio.query(
        "SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH AS len "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE DATA_TYPE = 'varchar' "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION", DRG)
    print(f"库内 varchar 列共 {len(cols)} 个，逐一检查是否含多字节数据：\n")
    bad = []
    for r in cols.itertuples():
        t, c = r.TABLE_NAME, r.COLUMN_NAME
        # DATALENGTH counts bytes in the column code page, LEN counts characters, so a
        # difference means multi-byte content. LIKE ranges are unreliable under a Chinese
        # collation, which made the first version of this check report false positives.
        try:
            q = ddlio.query(
                f"SELECT COUNT(1) AS n FROM [{t}] "
                f"WHERE DATALENGTH(RTRIM([{c}])) <> LEN(RTRIM([{c}]))", DRG)
            n = int(q.iloc[0]["n"] or 0)
            sample = ""
            if n:
                s = ddlio.query(f"SELECT TOP 1 [{c}] AS v FROM [{t}] "
                                f"WHERE DATALENGTH(RTRIM([{c}])) <> LEN(RTRIM([{c}]))", DRG)
                sample = str(s.iloc[0]["v"])[:40]
        except Exception:  # noqa: BLE001
            continue
        if n:
            bad.append((t, c, n, sample))
            print(f"  {t}.{c:<26} len={str(r.len):>5} 多字节行数 {n:>6}  样例 {sample!r}")
    print(f"\n结论：{len(bad)} 个 varchar 列含多字节（中文）数据 —— 这些列应为 nvarchar，"
          "否则写入即损坏")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只探测与出报告，不写库")
    ap.add_argument("--audit", action="store_true", help="检查 varchar 列是否含中文（编码体检）")
    a = ap.parse_args()
    if a.audit:
        return audit_charset()
    OUT_SUB.mkdir(parents=True, exist_ok=True)

    print("[1/2] 探测源数据新鲜度 ...")
    fr = probe()
    pg = pregroup_rate()
    rc = regress_cadence()

    md = report(fr, pg, rc)
    (OUT_SUB / "s19_freshness.md").write_text(md, encoding="utf-8")
    print(md)

    if a.dry:
        print("[dry] 未写入 drg 库")
        return 0

    print("[2/2] 写入 drg.ops_freshness / ops_alert ...")
    out = fr.copy()
    out["observed_at"] = datetime.now()
    for c in ("business_ts", "sync_ctrl_ts"):
        out[c] = pd.to_datetime(out[c], errors="coerce")
    ddlio.load_frame(out[["observed_at", "source_db", "source_table", "business_ts",
                          "rows_total", "lag_hours", "sync_ctrl_ts", "status"]],
                     "ops_freshness", truncate=True)
    write_alerts(fr, pg, rc)
    n = int(fr["status"].isin((STATUS_ALERT, STATUS_WARN)).sum())
    print(f"    异常/关注 {n} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
