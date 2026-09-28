"""S28 运行日志留存清理：ops_job_run 超期清理 + sys_audit_log 留存策略。

两类日志的留存方向**相反**，不能混为一谈：

1. `ops_job_run`（控制台触发的作业运行日志）——使用方定：留存上限 **30 天**，超期删除；
2. `sys_audit_log`（访问审计：登录/查看/导出/改密/告警确认/作业触发）——等保三级要求
   留存 **不少于 6 个月**，因此这里是**下限**：清理只允许按 ≥180 天执行，且**默认关闭**，
   必须显式传 `--audit-days` 才会删除（docs/35 D10）。归档方案：`s22_backup` 的逻辑备份
   已含 `sys_audit_log`，长期留存以备份介质为准。

用法：
    python src/etl/s28_joblog_purge.py                 # 只清理 ops_job_run 30 天前
    python src/etl/s28_joblog_purge.py --dry           # 只报数不删
    python src/etl/s28_joblog_purge.py --audit-days 180 --dry   # 顺带看审计日志留存状态
    python src/etl/s28_joblog_purge.py --audit-days 365        # 仅删 1 年前的审计记录

建议每日定时执行（Windows 任务计划，需 DRG_ALLOW_DDL=1）：
    schtasks /Create /TN "DRG-JobLog-Purge" ^
      /TR "cmd /c set DRG_ALLOW_DDL=1&& \"<python.exe 全路径>\" \"E:\\DRG\\src\\etl\\s28_joblog_purge.py\"" ^
      /SC DAILY /ST 03:00 /F

也可在信息科控制台「作业触发」页手动触发（key=s28，后端会注入 DRG_ALLOW_DDL）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402

DRG = "drg"
DEFAULT_DAYS = 30
# 等保三级：审计记录留存不少于 6 个月。这是下限（不得低于），不是上限（不是到点就删）。
AUDIT_MIN_DAYS = 180


def _count(table: str, where: str = "") -> int:
    sql = f"SELECT COUNT(*) n FROM {table}" + (f" WHERE {where}" if where else "")
    return int(ddlio.query(sql, DRG)["n"].iloc[0])


def purge_joblog(days: int, dry: bool) -> int:
    total = _count("ops_job_run")
    where = f"started_at < DATEADD(DAY, -{days}, GETDATE())"
    old = _count("ops_job_run", where)
    print(f"ops_job_run 现有 {total} 条；早于 {days} 天的 {old} 条")
    if dry or old == 0:
        return 0
    ddlio._exec(f"DELETE FROM ops_job_run WHERE {where}", DRG)  # noqa: SLF001
    print(f"    已删除 {old} 条，留存 {_count('ops_job_run')} 条")
    return old


def purge_audit_log(days: int, dry: bool) -> int:
    """Audit-log retention. Never below AUDIT_MIN_DAYS (等保三级 ≥6 个月)."""
    total = _count("sys_audit_log")
    span = ddlio.query("SELECT MIN(at) AS first_at, MAX(at) AS last_at FROM sys_audit_log", DRG)
    where = f"at < DATEADD(DAY, -{days}, GETDATE())"
    old = _count("sys_audit_log", where)
    print(f"sys_audit_log 现有 {total} 条；早于 {days} 天的 {old} 条")
    if len(span) and span.iloc[0]["first_at"] is not None:
        print(f"    记录跨度 {str(span.iloc[0]['first_at'])[:19]} ~ "
              f"{str(span.iloc[0]['last_at'])[:19]}")
    if dry or old == 0:
        return 0
    ddlio._exec(f"DELETE FROM sys_audit_log WHERE {where}", DRG)  # noqa: SLF001
    print(f"    已删除 {old} 条，留存 {_count('sys_audit_log')} 条")
    return old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"ops_job_run 留存天数，早于该天数的记录删除（默认 {DEFAULT_DAYS}）")
    ap.add_argument("--audit-days", type=int, default=None,
                    help=f"同时清理 sys_audit_log 中早于该天数的记录；下限 {AUDIT_MIN_DAYS} 天"
                         "（等保三级要求留存≥6 个月），**默认不清理**")
    ap.add_argument("--dry", action="store_true", help="只统计不删除")
    args = ap.parse_args()

    needs_write = not args.dry
    if needs_write and os.environ.get("DRG_ALLOW_DDL", "0") != "1":
        print("[!] 删除需 DRG_ALLOW_DDL=1（ddlio 写入门禁），请先设置后重跑")
        return 2

    if args.dry:
        purge_joblog(int(args.days), dry=True)
        if args.audit_days is not None:
            purge_audit_log(int(args.audit_days), dry=True)
        print("[dry] 未删除")
        return 0

    purge_joblog(int(args.days), dry=False)

    if args.audit_days is not None:
        audit_days = int(args.audit_days)
        if audit_days < AUDIT_MIN_DAYS:
            print(f"[!] --audit-days={audit_days} 低于等保三级下限 {AUDIT_MIN_DAYS} 天，已拒绝"
                  "（审计记录留存不得少于 6 个月）")
            return 2
        purge_audit_log(audit_days, dry=False)
    else:
        print("sys_audit_log 未清理（默认保留；如需清理请显式 --audit-days ≥ "
              f"{AUDIT_MIN_DAYS}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
