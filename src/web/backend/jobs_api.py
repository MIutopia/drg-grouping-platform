"""One-click job triggers (IT console).

Security note: strict allowlist - the client may only pass a job key, never a
command or arguments; commands are fixed server-side in the JOBS table. Jobs run
in a background thread + subprocess; the endpoint returns a run_id immediately and
the frontend polls the log. DB-writing jobs get DRG_ALLOW_DDL=1 injected here
(same gate as s18 --load). Restricted to role='it'. A job cannot be re-triggered
while it is still running.

Run records are persisted to drg.ops_job_run (no longer only in-process memory,
so restarts keep history) and purged by s28_joblog_purge.py after the retention
limit (default 30 days).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

import db
from auth import require_roles

router = APIRouter(prefix="/api/ops/jobs", tags=["jobs"])
IT = require_roles("it")
ROOT = Path(__file__).resolve().parents[3]          # E:\DRG

# Allowlist: only the jobs listed here can be triggered
JOBS: dict[str, dict] = {
    "daily": {"script": "src/run/daily_insim.py", "args": [], "ddl": True,
              "label": "在院快照 + 装载（s06 + s18 --load）"},
    "s19": {"script": "src/etl/s19_freshness.py", "args": [], "ddl": True,
            "label": "数据新鲜度与告警（s19）"},
    "s20": {"script": "src/etl/s20_regress.py", "args": ["--note", "控制台触发"], "ddl": True,
            "label": "回归门槛判定（s20）"},
    "s21": {"script": "src/etl/s21_policy_watch.py", "args": [], "ddl": True,
            "label": "政策文件扫描（s21）"},
    "s27": {"script": "src/etl/s27_import_doctor.py", "args": [], "ddl": True,
            "label": "医师花名册导入（s27）"},
    "s28": {"script": "src/etl/s28_joblog_purge.py", "args": [], "ddl": True,
            "label": "作业日志留存清理（s28，>30 天）"},
    "s25": {"script": "src/etl/s25_secret_scan.py", "args": [], "ddl": False,
            "label": "敏感信息扫描（s25）"},
    "s26": {"script": "src/etl/s26_pii_mask.py", "args": [], "ddl": False,
            "label": "产出物脱敏副本（s26）"},
}

_runs: dict[str, dict] = {}
_lock = threading.Lock()
_MAXKEEP = 40


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _iso(dt) -> str | None:
    """datetime -> 'YYYY-MM-DD HH:MM:SS' (same format as _now(); shown as-is in the frontend)."""
    if dt is None or dt == "":
        return None
    s = str(dt).replace("T", " ")
    return s[:19]


def _load_recent(attempts: int = 3) -> bool:
    """Load recent run records from the database back into memory at startup so
    history survives restarts.

    docs/35 D14: when this query failed right after a container restart the endpoint
    silently reported an empty history although the rows were there moments later. Retry a
    few times, log the failure instead of hiding it, and let /runs re-attempt lazily.
    """
    for attempt in range(1, attempts + 1):
        try:
            rows = db.records(db.query(
                f"SELECT TOP {_MAXKEEP} run_id, job_key, job_label, status, returncode, "
                "started_at, ended_at, triggered_by, log FROM ops_job_run "
                "ORDER BY started_at DESC"))
        except Exception as e:                                  # noqa: BLE001
            print(f"[warn] 作业历史加载失败（第 {attempt}/{attempts} 次）：{str(e)[:120]}")
            if attempt == attempts:
                return False
            time.sleep(0.5 * attempt)
            continue
        for r in rows:
            _runs[r["run_id"]] = {
                "run_id": r["run_id"], "key": r["job_key"], "label": r["job_label"] or "",
                "status": r["status"], "started_at": _iso(r["started_at"]),
                "ended_at": _iso(r["ended_at"]), "returncode": r["returncode"],
                "log": r["log"] or "", "by": r["triggered_by"] or "",
            }
        return True
    return False


def _persist_start(run_id: str, key: str, by: str) -> None:
    db.execute("INSERT INTO ops_job_run (run_id, job_key, job_label, status, started_at, "
               "triggered_by) VALUES (%s, %s, %s, %s, %s, %s)",
               (run_id, key, JOBS[key]["label"], "running", _now(), by))


def _persist_end(run_id: str, status: str, returncode: int | None, log: str) -> None:
    db.execute("UPDATE ops_job_run SET status=%s, returncode=%s, ended_at=%s, log=%s "
               "WHERE run_id=%s", (status, returncode, _now(), log, run_id))


def _exec(run_id: str, key: str) -> None:
    j = JOBS[key]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if j["ddl"]:
        env["DRG_ALLOW_DDL"] = "1"
    try:
        p = subprocess.run(
            [sys.executable, str(ROOT / j["script"]), *j["args"]], cwd=str(ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=j.get("timeout", 1800))
        out = (p.stdout or "")[-12000:]
        err = (p.stderr or "")[-3000:]
        status = "ok" if p.returncode == 0 else "fail"
        log = out + (("\n[stderr]\n" + err) if err.strip() else "")
        with _lock:
            _runs[run_id].update(status=status, returncode=p.returncode,
                                 ended_at=_now(), log=log)
        _persist_end(run_id, status, p.returncode, log)
    except subprocess.TimeoutExpired:
        with _lock:
            _runs[run_id].update(status="timeout", ended_at=_now(), log="作业超时（>30 分钟）")
        _persist_end(run_id, "timeout", None, "作业超时（>30 分钟）")
    except Exception as e:                                       # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        with _lock:
            _runs[run_id].update(status="error", ended_at=_now(), log=msg)
        _persist_end(run_id, "error", None, msg)


@router.get("")
def list_jobs(user: dict = Depends(IT)) -> dict:
    running = {r["key"] for r in _runs.values() if r["status"] == "running"}
    return {"jobs": [{"key": k, "label": v["label"], "running": k in running}
                     for k, v in JOBS.items()]}


@router.post("/run/{key}")
def run_job(key: str, user: dict = Depends(IT)) -> dict:
    if key not in JOBS:
        raise HTTPException(404, "未知作业")
    with _lock:
        if any(r["key"] == key and r["status"] == "running" for r in _runs.values()):
            raise HTTPException(409, "该作业正在运行中，请稍候")
        run_id = uuid.uuid4().hex[:12]
        _runs[run_id] = {"run_id": run_id, "key": key, "label": JOBS[key]["label"],
                         "status": "running", "started_at": _now(), "ended_at": None,
                         "returncode": None, "log": "", "by": user["user_name"]}
    _persist_start(run_id, key, user["user_name"])
    db.audit(user["login_phone"], "run_job", target=key)
    threading.Thread(target=_exec, args=(run_id, key), daemon=True).start()
    return {"run_id": run_id}


@router.get("/runs")
def list_runs(user: dict = Depends(IT)) -> list[dict]:
    if not _runs:
        _load_recent()          # startup may have raced a restarting database (docs/35 D14)
    return sorted(_runs.values(), key=lambda r: r["started_at"], reverse=True)[:_MAXKEEP]


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(IT)) -> dict:
    r = _runs.get(run_id)
    if not r:
        raise HTTPException(404, "无此运行记录")
    return r


_load_recent()
