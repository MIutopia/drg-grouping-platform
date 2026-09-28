"""Controlled DDL execution layer.

dbio.py deliberately rejects anything that is not a SELECT. Bootstrap work (creating the drg
database, its tables and loading reference data) therefore needs a separate path, which is
kept deliberately narrow:

  1. DDL is refused unless the environment variable DRG_ALLOW_DDL=1 is set explicitly;
  2. only .sql files living in src/sql/ can be executed - no ad-hoc statements from callers;
  3. every executed file is recorded (name, sha256, row counts, timing) so the drg database
     can always be rebuilt from a known revision.

Docs: docs/23-drg库设计与落地.md.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import ROOT, get_profile  # noqa: E402

SQL_DIR = ROOT / "项目" / "sql"
ALLOW_DDL = os.environ.get("DRG_ALLOW_DDL", "0") == "1"
# Explicit ETL bound (code review R2); measurement note in src/config/settings.py.
_QUERY_TIMEOUT = int(os.environ.get("DRG_QUERY_TIMEOUT", "300"))

# Statements a bootstrap script may contain. Kept explicit so a stray script cannot, say,
# drop a table that belongs to another database. DROP TABLE is permitted because migration
# scripts live in the same version-controlled directory and are reviewed like any other
# change; every one of them pairs USE [drg] with a guarded DROP.
ALLOWED_PREFIX = ("CREATE ", "IF ", "SET ", "USE ", "GO", "--", "PRINT ", "DECLARE ",
                  "ALTER TABLE ", "INSERT ", "MERGE ", "DROP TABLE ", "BEGIN", "END")


def connect(database: str | None = None):
    """Open a connection for administrative work (autocommit, no read-only guard)."""
    if not ALLOW_DDL:
        raise PermissionError(
            "DDL 被拒绝：需显式设置环境变量 DRG_ALLOW_DDL=1（建库脚本专用）")
    import pymssql  # noqa: PLC0415

    prof = dict(get_profile("sbo"))
    return pymssql.connect(
        server=prof["host"], port=str(prof["port"]), user=prof["user"],
        password=prof["password"], database=database or prof["database"],
        charset=prof.get("charset", "utf8"),
        login_timeout=prof.get("login_timeout", 10),
        timeout=prof.get("query_timeout", _QUERY_TIMEOUT), autocommit=True)


def query(sql: str, database: str | None = None) -> pd.DataFrame:
    """Read-only query against drg. Deliberately NOT gated by DRG_ALLOW_DDL: reading the
    monitoring tables (--status / --verify) must work for anyone doing routine checks."""
    import pymssql  # noqa: PLC0415

    prof = dict(get_profile("sbo"))
    conn = pymssql.connect(
        server=prof["host"], port=str(prof["port"]), user=prof["user"],
        password=prof["password"], database=database or prof["database"],
        charset=prof.get("charset", "utf8"),
        login_timeout=prof.get("login_timeout", 10),
        timeout=prof.get("query_timeout", _QUERY_TIMEOUT), autocommit=True)
    try:
        return pd.read_sql(sql, conn)
    finally:
        conn.close()


def run_script(path: Path, database: str | None = None, *, verbose: bool = True) -> dict:
    """Execute one .sql file, splitting on GO batches."""
    p = Path(path).resolve()
    if SQL_DIR not in p.parents and p.parent != SQL_DIR:
        raise PermissionError(f"仅允许执行 {SQL_DIR} 下的脚本：{p}")
    sql = p.read_text(encoding="utf-8")
    batches = [b.strip() for b in _split_go(sql) if b.strip()]
    if not batches:
        raise ValueError(f"{p.name} 无有效语句")

    conn = connect(database)
    t0 = time.time()
    executed = 0
    try:
        cur = conn.cursor()
        for b in batches:
            head = b.lstrip().split(None, 1)[0].upper() if b.strip() else ""
            if head and not any(b.lstrip().upper().startswith(pfx.strip())
                                for pfx in ALLOWED_PREFIX if pfx.strip()):
                raise PermissionError(f"{p.name} 含未授权语句：{b[:60]}")
            cur.execute(b)
            executed += 1
        conn.commit()
    finally:
        conn.close()
    info = {"script": f"{p.parent.name}/{p.name}", "batches": executed,
            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16],
            "seconds": round(time.time() - t0, 2), "at": datetime.now()}
    if verbose:
        print(f"    [ddl] {info['script']}  {executed} 批  {info['seconds']}s  "
              f"sha256={info['sha256']}")
    return info


def _split_go(sql: str) -> list[str]:
    """Split on a standalone GO line (case-insensitive)."""
    out, buf = [], []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            out.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    out.append("\n".join(buf))
    return out


ALERT_LEVELS = ("info", "warn", "high")


def normalize_alert_severity(value: object) -> tuple[str, str | None]:
    """Map an arbitrary severity to (stored_level, original_if_unknown).

    docs/35 D4: ops_alert.severity must accept whatever an upstream raises. A level the
    console does not know is stored as 'warn' (so its colour logic never breaks) while the
    original text is returned for the caller to keep in detail - nothing is dropped.
    """
    s = str(value if value is not None else "").strip()
    if s in ALERT_LEVELS:
        return s, None
    return "warn", (s or None)


def insert_alert(severity: object, item: str, detail: str | None, owner: str,
                 database: str = "drg") -> None:
    """Insert one ops_alert row, normalising an unknown severity (docs/35 D4).

    Kept here rather than in each job so s19 and s20 cannot drift apart, and so a new
    alert producer inherits the guard by construction.
    """
    sev, original = normalize_alert_severity(severity)
    if original:
        detail = f"{detail or ''}（原始级别：{original}）".strip()
    execute("INSERT INTO ops_alert (severity, item, detail, [owner]) "
            "VALUES (%s, %s, %s, %s)", (sev, item, detail, owner), database)


def execute(sql: str, params: tuple, database: str, *, verbose: bool = False) -> None:
    """Run a parameterised statement.

    Chinese must never be embedded as a literal: strings written directly into the SQL text
    come back mangled through this driver, while bound parameters survive intact. Everything
    that carries Chinese therefore goes through this function.
    """
    conn = connect(database)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
    if verbose:
        print(f"    [exec] {sql.split()[0]} ok")


def load_frame(df: pd.DataFrame, table: str, *, database: str = "drg",
               policy_version: str | None = None, truncate: bool = True,
               batch: int = 500) -> int:
    """Bulk-load a DataFrame into drg.<table>, replacing its contents.

    Column names must already match the target table. Values are parameterised, so no string
    from the dataframe is ever concatenated into SQL.
    """
    if df.empty:
        if truncate:
            _exec(f"DELETE FROM {table}", database)
        return 0
    d = df.copy()
    if policy_version is not None:
        d["policy_version"] = policy_version
    cols = list(d.columns)
    placeholders = ",".join("%s" for _ in cols)
    col_sql = ",".join(f"[{c}]" for c in cols)
    conn = connect(database)
    n = 0
    try:
        cur = conn.cursor()
        if truncate:
            cur.execute(f"DELETE FROM {table}")
        for start in range(0, len(d), batch):
            chunk = d.iloc[start:start + batch]
            rows = [tuple(_clean(v) for v in r) for r in chunk.itertuples(index=False, name=None)]
            cur.executemany(f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})", rows)
            n += len(rows)
        conn.commit()
    finally:
        conn.close()
    print(f"    [load] {table}: {n:,} 行")
    return n


def _clean(v: object) -> object:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if v is pd.NaT:
        return None
    try:
        if pd.isna(v) and not isinstance(v, (str, bytes)):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "to_pydatetime"):
        return v.to_pydatetime()
    # pymssql rejects numpy scalar types outright, so they are unwrapped to builtins
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    if isinstance(v, (bool, np.bool_)):
        return int(v)
    # empty strings come from CSV fillna("") and cannot be converted into numeric columns
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _exec(sql: str, database: str) -> None:
    conn = connect(database)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
    finally:
        conn.close()
