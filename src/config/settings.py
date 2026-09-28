"""Path constants and database connection profiles.

Credentials are never committed: env vars override src/config/local.json.
Read-only usage only. Full convention: docs/15-代码模块说明与作业手册.md §二.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

try:                       # 凭据解密（同目录 secure.py：Windows DPAPI / Fernet）
    import secure as _secure
except Exception:          # noqa: BLE001 - 模块不可用时退化为明文读取，不阻断作业
    _secure = None

# ------------------------------------------------------------------
# Console encoding guard (docs/35 §八·补4.3)
# ------------------------------------------------------------------
# Reports contain characters outside the host console's GBK codepage (↔ U+2194, − U+2212,
# ✅, ¡ in an example string...). Printing one raised UnicodeEncodeError *after* the job had
# already produced its output, so a fully successful job exited non-zero and looked failed -
# observed twice (s07, s08). Every job imports this module at start, so the guard lives here
# once instead of in each entry point (11 files carry such characters today).
# It keeps the console's own encoding - Chinese still renders as before - and only replaces
# the few characters it cannot represent. The job runner already sets PYTHONIOENCODING=utf-8
# (jobs_api.py); this is the belt to that pair of braces.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:          # noqa: BLE001 - redirected/piped streams have no reconfigure
    pass

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]          # e:\DRG
CONFIG_DIR = ROOT / "项目" / "config"
ETL_DIR = ROOT / "项目" / "etl"
OUT_DIR = ROOT / "项目" / "out"
RUN_DIR = ROOT / "项目" / "run"
RAW_DIR = ROOT / "原始资料"
DICT_DIR = RAW_DIR / "数据字典"

for _d in (OUT_DIR, RUN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------
# Connection profiles
#   sbo  : 192.0.2.10  HIS / warehouse primary (SBO), SQL Server 2014 SP3
#   dict : 192.0.2.11  dictionary node (peer replication with .6), SQL Server 2008 R2
# ------------------------------------------------------------------
PROFILES: dict[str, dict[str, Any]] = {
    "sbo": {
        "host": "192.0.2.10",
        "port": 1433,
        "user": "svc_ro",
        "password": "",
        "database": "SBO",
        "charset": "utf8",
        "login_timeout": 10,
        # Explicit ETL bound (code review R2). Previously an unstated 600s, so a single stuck
        # query held a batch job for ten minutes unnoticed. Measured on the live instance
        # (2026-09-27) the slowest query is the s07 fee aggregation at 0.6s and the TCM
        # subject aggregation at 0.6s, i.e. 300s is ~500x the observed worst case: anything
        # past it is a hang, not a slow query. Jobs that genuinely need longer set
        # DRG_QUERY_TIMEOUT. The web layer keeps its own much shorter timeouts (db.py) and
        # does not read this key.
        "query_timeout": 300,
    },
    "dict": {
        "host": "192.0.2.11",
        "port": 1433,
        "user": "svc_ro",
        "password": "",
        "database": "SBO",
        "charset": "utf8",
        "login_timeout": 10,
        "query_timeout": 300,      # see the note on the sbo profile (R2)
    },
}

_LOCAL_JSON = CONFIG_DIR / "local.json"


def _load_local() -> dict[str, dict[str, Any]]:
    if not _LOCAL_JSON.exists():
        return {}
    try:
        data = json.loads(_LOCAL_JSON.read_text(encoding="utf-8"))
    except Exception:  # a broken config file must not abort a job
        return {}
    return data if isinstance(data, dict) else {}


_WARNED: set[str] = set()


def _unprotect(name: str, value: object) -> str:
    """口令若是密文（dpapi1:/fernet1: 前缀）则解密；明文则原样返回并告警一次。"""
    if not value:
        return ""
    if _secure is not None and _secure.is_protected(value):
        return _secure.unprotect(str(value))
    if name not in _WARNED:
        _WARNED.add(name)
        print(f"[warn] 连接档案 {name} 的口令在配置中是**明文**，建议改为密文存储："
              f"`python src/etl/s29_credential.py protect`")
    return str(value)


def get_profile(name: str = "sbo") -> dict[str, Any]:
    """Return a profile with local.json and env-var overrides applied."""
    if name not in PROFILES:
        raise KeyError(f"未知连接档案: {name}，可选 {sorted(PROFILES)}")

    prof = dict(PROFILES[name])
    local = _load_local().get(name) or {}
    prof.update({k: v for k, v in local.items() if v not in (None, "")})

    prefix = f"DRG_{name.upper()}_"
    for field in ("host", "port", "user", "password", "database", "charset"):
        env = os.environ.get(prefix + field.upper())
        if env:
            prof[field] = int(env) if field == "port" else env

    prof["password"] = _unprotect(name, prof.get("password"))
    return prof


def describe(name: str = "sbo") -> str:
    """Masked connection description for logs."""
    p = get_profile(name)
    pwd_state = "已设置" if p.get("password") else "未设置"
    return (
        f"{name}: {p['user']}@{p['host']}:{p['port']}/{p['database']} "
        f"(口令 {pwd_state})"
    )
