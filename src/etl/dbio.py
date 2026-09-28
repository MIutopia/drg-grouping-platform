"""Read-only access layer for the SBO source database.

Write statements are rejected by design. SQL must stay 2014-compatible.
Docs: docs/15-代码模块说明与作业手册.md §三.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings import describe, get_profile  # noqa: E402

import srcschema  # noqa: E402  源库表名逻辑映射（唯一的展开点，见 srcschema.py）

_CONN_CACHE: dict[str, Any] = {}
_BACKEND: dict[str, str] = {}

# Source varchar columns may arrive as latin-1 decoded GBK bytes; restore by default.
FIX_MOJIBAKE = os.environ.get("DRG_FIX_MOJIBAKE", "1") != "0"

_BANNED = ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "TRUNCATE",
           "EXEC", "CREATE", "GRANT", "REVOKE", "BACKUP", "RESTORE")

# Explicit timeouts (code review R2). The fallback used to be a bare literal 600s, so a single
# stuck query could hold a batch job for ten minutes with nothing in the logs - the same
# unbounded-timeout pattern behind the web-layer outage defect (docs/35 D13). Profiles can
# still override per job, and a long aggregation can raise DRG_QUERY_TIMEOUT.
_LOGIN_TIMEOUT = int(os.environ.get("DRG_LOGIN_TIMEOUT", "10"))
# Measured worst case on the live instance is 0.6s (s07 fee aggregation, TCM subject
# aggregation), so 300s is the bound rather than the old bare 600s - see settings.PROFILES.
_QUERY_TIMEOUT = int(os.environ.get("DRG_QUERY_TIMEOUT", "300"))


# ------------------------------------------------------------------
# Connection
# ------------------------------------------------------------------
def _connect_pymssql(prof: dict[str, Any]):
    import pymssql

    return pymssql.connect(
        server=prof["host"],
        port=str(prof["port"]),
        user=prof["user"],
        password=prof["password"],
        database=prof["database"],
        charset=prof.get("charset", "utf8"),
        login_timeout=prof.get("login_timeout", _LOGIN_TIMEOUT),
        timeout=prof.get("query_timeout", _QUERY_TIMEOUT),
        autocommit=True,
    )


def _connect_pyodbc(prof: dict[str, Any]):
    import pyodbc

    cs = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={prof['host']},{prof['port']};"
        f"DATABASE={prof['database']};"
        f"UID={prof['user']};PWD={prof['password']};"
        "Encrypt=no;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(cs, timeout=prof.get("login_timeout", _LOGIN_TIMEOUT), autocommit=True)


def connect(profile: str = "sbo", *, fresh: bool = False):
    """Return a reusable connection, cached per profile unless fresh=True."""
    if not fresh and profile in _CONN_CACHE:
        return _CONN_CACHE[profile]

    prof = get_profile(profile)
    if not prof.get("password"):
        raise RuntimeError(
            f"{describe(profile)} 口令未设置。请通过环境变量 "
            f"DRG_{profile.upper()}_PASSWORD 或 src/config/local.json 配置。"
        )

    errors: list[str] = []
    for name, fn in (("pymssql", _connect_pymssql), ("pyodbc", _connect_pyodbc)):
        try:
            conn = fn(prof)
            _BACKEND[profile] = name
            if not fresh:
                _CONN_CACHE[profile] = conn
            return conn
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    raise RuntimeError(f"连接 {describe(profile)} 失败：\n  " + "\n  ".join(errors))


def backend_of(profile: str = "sbo") -> str:
    return _BACKEND.get(profile, "-")


def close_all() -> None:
    for conn in list(_CONN_CACHE.values()):
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _CONN_CACHE.clear()


# ------------------------------------------------------------------
# Read-only queries
# ------------------------------------------------------------------
# String literals, line comments and block comments - blanked out before the statement is
# inspected, so that data (`LIKE 'a;b'`), a comment (`-- 不删数据`) or a quoted keyword can
# never be mistaken for code.
_LITERAL_OR_COMMENT = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


def _strip_literals(sql: str) -> str:
    """Replace string literals and comments with spaces, keeping offsets intact."""
    return _LITERAL_OR_COMMENT.sub(lambda m: " " * len(m.group(0)), sql)


def _guard_readonly(sql: str) -> None:
    """Allow one read-only statement, and nothing that could hide a write behind it.

    Two gaps are closed here (code review Q2): the previous check only looked at the leading
    keyword, so `SELECT 1; DROP TABLE x` passed untouched, and a banned keyword appearing
    later in the statement was never inspected. Scanning happens on a copy with literals and
    comments blanked out, so legitimate data such as `LIKE 'a;b'` still passes.
    """
    code = _strip_literals(sql).strip().rstrip(";").strip()
    if not code:
        raise PermissionError("本层仅允许只读查询，已拦截空语句")
    if ";" in code:
        raise PermissionError(f"本层仅允许单条只读查询，已拦截多语句：{sql[:80]}")
    head = code.lstrip("(").lstrip().upper()
    up = code.upper()
    for kw in _BANNED:
        if head.startswith(kw) or re.search(rf"\b{kw}\b", up):
            raise PermissionError(f"本层仅允许只读查询，已拦截：{kw} ... {sql[:80]}")


def fix_mojibake(s: str) -> str:
    """Restore GBK text wrongly decoded as latin-1 (e.g. 'Ï¥¹Ø½Ú²¡' -> '膝关节病').

    Replaces only when the result contains clearly more CJK characters, so that
    healthy strings are never damaged.
    """
    if not s or not isinstance(s, str) or s.isascii():
        return s
    try:
        fixed = s.encode("latin-1").decode("gbk")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s
    if fixed == s:
        return s

    def cjk(t: str) -> int:
        return sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")

    return fixed if cjk(fixed) > cjk(s) else s


def fix_mojibake_df(df: pd.DataFrame) -> pd.DataFrame:
    """Apply fix_mojibake() to text columns.

    pandas 3.x uses a dedicated `str` dtype instead of `object`, so text columns
    are detected as "not numeric / datetime / bool" rather than by dtype == object.
    """
    if not FIX_MOJIBAKE or df.empty:
        return df
    for col in df.columns:
        s = df[col]
        if (pd.api.types.is_numeric_dtype(s)
                or pd.api.types.is_datetime64_any_dtype(s)
                or pd.api.types.is_bool_dtype(s)):
            continue
        sample = s.dropna().astype(str).head(50)
        if sample.empty or not any(not str(v).isascii() for v in sample):
            continue
        df[col] = s.map(lambda v: fix_mojibake(v) if isinstance(v, str) else v)
    return df


def query_df(
    sql: str,
    profile: str = "sbo",
    *,
    params: Sequence[Any] | None = None,
    chunksize: int | None = None,
    verbose: bool = True,
) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame.

    源表(key) 令牌在此展开为物理表名：这是全项目唯一的展开点（换现场只改
    config/source_schema.json，不动任何 SQL 文本）。
    """
    sql = srcschema.expand(sql)
    _guard_readonly(sql)
    t0 = time.time()
    conn = connect(profile)
    if chunksize:
        parts = list(pd.read_sql(sql, conn, params=params, chunksize=chunksize))
        df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    else:
        df = pd.read_sql(sql, conn, params=params)
    df = fix_mojibake_df(df)
    elapsed = time.time() - t0
    if verbose and elapsed > 3:
        print(f"    [db:{backend_of(profile)}] {elapsed:6.1f}s  {len(df):>8,} 行  {sql[:70]}...")
    return df


def query_rows(sql: str, profile: str = "sbo", *, params: Sequence[Any] | None = None) -> list[tuple]:
    """Run a SELECT and return raw tuples (lighter than query_df)."""
    sql = srcschema.expand(sql)
    _guard_readonly(sql)
    cur = connect(profile).cursor()
    try:
        cur.execute(sql, tuple(params) if params else None)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def scalar(sql: str, profile: str = "sbo", *, params: Sequence[Any] | None = None) -> Any:
    rows = query_rows(sql, profile, params=params)
    return rows[0][0] if rows else None


def server_info(profile: str = "sbo") -> dict[str, Any]:
    """Server version and current database, for audit trail."""
    sql = """
    SELECT @@VERSION AS version,
           CAST(SERVERPROPERTY('ProductLevel') AS varchar(32)) AS sp_level,
           CAST(SERVERPROPERTY('Edition') AS varchar(64))  AS edition,
           DB_NAME() AS current_db,
           SUSER_SNAME() AS login_name,
           GETDATE() AS server_time
    """
    df = query_df(sql, profile, verbose=False)
    return df.iloc[0].to_dict() if len(df) else {}


def table_exists(table: str, profile: str = "sbo", schema: str = "dbo") -> bool:
    n = scalar(
        "SELECT COUNT(1) FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        profile, params=(schema, srcschema.expand(table)),
    )
    return bool(n)


def list_tables(like: str = "%", profile: str = "sbo") -> pd.DataFrame:
    """列出基表**与视图**。

    源库里若干一期依赖的对象是视图而非基表（HIS 侧的封装视图）；原先只查
    TABLE_TYPE='BASE TABLE'，会让存在性检查把可用对象误报为"缺失"（s00 节 2）。
    """
    return query_df(
        "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE IN ('BASE TABLE','VIEW') AND TABLE_NAME LIKE %s ORDER BY TABLE_NAME",
        profile, params=(srcschema.expand(like),), verbose=False,
    )


def row_counts(tables: Sequence[str], profile: str = "sbo") -> pd.DataFrame:
    """Row counts via sys.partitions, avoiding a full table scan."""
    out = []
    for t in tables:
        try:
            t = srcschema.expand(t)
            n = scalar(
                "SELECT SUM(p.rows) FROM sys.partitions p "
                "JOIN sys.objects o ON o.object_id = p.object_id "
                "WHERE o.name = %s AND p.index_id IN (0,1)",
                profile, params=(t.split(".")[-1],),
            )
            out.append({"table": t, "rows": int(n or 0)})
        except Exception as exc:  # noqa: BLE001
            out.append({"table": t, "rows": -1, "error": str(exc)[:120]})
    return pd.DataFrame(out)
