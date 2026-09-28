"""S22 Back up the drg database as a reproducible logical dump.

docs/23 lists "备份策略：drg 库尚未纳入备份" as a P0. The drg database is largely rebuildable
from src/config/dict and src/out via s18 --load, but two things are NOT rebuildable and must
be captured:

  1. the ops_* runtime history (ops_ddl_log / ops_run_log / ops_regress_run / ops_alert) - the
     audit trail that proves what ran when;
  2. any state not yet written back to the offline CSVs.

This job dumps every drg table to CSV under src/out/backup/<timestamp>/ using the read-only
path (ddlio.query), so it works with the least-privilege account.

It is a LOGICAL backup, not a native one: it cannot do point-in-time recovery or restore the
database file-by-file. Native `BACKUP DATABASE [drg] TO DISK=...` still has to be scheduled by
the DBA (信息科), because it needs db_backupoperator and a path on the server - neither of which
a routine read-only account has. Both layers are listed in the report.
Docs: docs/23-drg库设计与落地.md §五.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402

DRG = "drg"
OUT_SUB = OUT_DIR / "backup"


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def tables() -> list[str]:
    return [str(n) for n in ddlio.query("SELECT name FROM sys.tables ORDER BY name", DRG)["name"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=0, help="保留最近 N 份备份，0=不清理")
    a = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = OUT_SUB / ts
    dest.mkdir(parents=True, exist_ok=True)

    tabs = tables()
    print(f"[s22] drg 库 {len(tabs)} 张表 → `{dest}`")
    manifest_rows, total = [], 0
    for t in tabs:
        df = ddlio.query(f"SELECT * FROM [{t}]", DRG)
        df.to_csv(dest / f"{t}.csv", index=False, encoding="utf-8-sig")
        manifest_rows.append({"table": t, "rows": len(df)})
        total += len(df)
    manifest = pd.DataFrame(manifest_rows)
    manifest.to_csv(dest / "_manifest.csv", index=False, encoding="utf-8-sig")

    if a.keep:
        dirs = sorted(OUT_SUB.glob("*/"), reverse=True)
        for old in dirs[a.keep:]:
            shutil.rmtree(old, ignore_errors=True)
            print(f"      清理旧备份 {old.name}")

    md = ["# S22 drg 库逻辑备份", "",
          f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
          f"- 目标目录：`{dest}`（{len(tabs)} 张表 / {total:,} 行）", "",
          "## 一、本次备份", "", _md(manifest), "",
          "## 二、备份策略（两层）", "",
          "| 层 | 方式 | 负责 | 说明 |",
          "|---|---|---|---|",
          "| **逻辑备份** | `s22_backup.py`（本作业） | 项目组 | 逐表导出 CSV，只读账号即可；恢复 = `s18 --load` 或手工导入 |",
          "| **原生备份** | `BACKUP DATABASE [drg] TO DISK=...` | **信息科/DBA** | 需 `db_backupoperator` + 服务器路径，本账号无权；支持时点恢复 |",
          "| 代码与字典 | git | 项目组 | `src/config` / `sql` / `etl` / `docs` 已入库 |",
          "| 离线产出 | `src/out` | 项目组 | **未入版本库**，建议随逻辑备份归档 |", "",
          "> 提示：`ops_*` 运行历史（DDL 留痕 / 作业日志 / 回归历史 / 告警）**只能**靠本备份或原生备份保留，",
          "> `out/` 目录重建不出这些。建议本作业纳入日调度，原生备份由信息科按使用方灾备规范排期。", ""]
    (dest / "s22_backup_report.md").write_text("\n".join(md), encoding="utf-8")
    print(md[0])
    print(f"      合计 {total:,} 行；报告 → {dest / 's22_backup_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
