"""每日在院快照作业：s06 生成当日四行清单 → s18 --load 落库 drg.result_insim。

由 Windows 任务计划每天定时调用（信息科注册）：
    schtasks /Create /TN "DRG-Daily-InSim" ^
      /TR "\"<python.exe 全路径>\" \"E:\\DRG\\src\\run\\daily_insim.py\"" ^
      /SC DAILY /ST 08:00 /F

口令来自 src/config/local.json（.gitignore 已排除），不写入本文件、也不写进命令行。
改用 Python 而非 .ps1 的原因：Windows PowerShell 无 BOM 时按 GBK 读 .ps1，中文路径会被损坏；
Python 源码默认 UTF-8，天然安全。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # E:\DRG
ETL = ROOT / "项目" / "etl"
LOG = ROOT / "项目" / "run" / "daily_insim.log"


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")


def run(args: list[str]) -> None:
    env = os.environ.copy()
    env["DRG_ALLOW_DDL"] = "1"          # s18 --load 的写入门禁
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with open(LOG, "ab") as fh:          # 子进程输出按 UTF-8 字节原样追加
        fh.write(r.stdout or b"")
    if r.returncode != 0:
        raise RuntimeError(f"{Path(args[0]).name} exit code {r.returncode}")


def main() -> int:
    log("=== START ===")
    try:
        run([str(ETL / "s06_insim.py")])
        run([str(ETL / "s18_create_drg_db.py"), "--load"])
        log("=== DONE ===")
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"FAIL: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
