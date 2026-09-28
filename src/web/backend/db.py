"""Database access layer: reuses the connection profiles in src/config/settings.py,
read-mostly.

The drg database (owned by this platform) lives on the same instance as SBO/HIS and
is selected via the database parameter; every query in this platform targets drg,
never HIS.

Connections are pooled (a queue per database) instead of opened per request: docs/35 D6
measured connection setup as the dominant cost of the doctor workbench (P95 1.33s against a
1.0s target). A pooled connection may have been closed by the server while idle, so every
call retries once on a fresh connection before failing.

Outage behaviour is bounded on purpose (docs/35 D13). FastAPI runs these sync endpoints in
a finite worker-thread pool, so a stopped database must never hold threads for long.

The first attempt at that only handled "while the database is down" (short login/query
timeouts + a circuit breaker) and left "after it comes back" broken: pooled connections
opened before the outage are dead, and handing one out made the request hang inside
pymssql - the DB-Lib timeouts do NOT bound a blackholed TCP socket, so threads piled up
until /api/health itself could no longer answer. The test stand reproduced exactly that:
180s after recovery the service was still unhealthy and needed a container restart.

Three guards now close that path (all three are needed - removing any one reopens it):
  1. every connect runs under a hard wall-clock cap (`_CONNECT_CAP`), independent of the
     DB-Lib/OS timeouts;
  2. every call runs under a hard wall-clock cap (`_CALL_CAP`), so a hung connection can
     never hold a worker thread indefinitely;
  3. a pooled connection that has been idle longer than `_VALIDATE_IDLE` is revalidated
     before reuse (bounded, so the check itself cannot hang), and the whole pool is
     rebuilt whenever the breaker goes from open back to closed - i.e. exactly once, on
     the first sign that the data source is serving again.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))
from settings import get_profile  # noqa: E402

# Pool sizing and failure handling are environment-tunable so the outage drill can be
# reproduced (docs/35 D13) without editing code.
_POOL_SIZE = int(os.environ.get("DRG_DB_POOL", "16"))
_LOGIN_TIMEOUT = int(os.environ.get("DRG_DB_LOGIN_TIMEOUT", "5"))
_QUERY_TIMEOUT = int(os.environ.get("DRG_DB_TIMEOUT", "15"))
_ACQUIRE_TIMEOUT = float(os.environ.get("DRG_DB_ACQUIRE_TIMEOUT", "2"))
_BREAK_AFTER = int(os.environ.get("DRG_DB_BREAK_AFTER", "3"))
_BREAK_COOLDOWN = float(os.environ.get("DRG_DB_BREAK_COOLDOWN", "15"))
# Hard wall-clock caps (docs/35 D13). DB-Lib's login_timeout/query timeout do not bound a
# blackholed TCP connection (a stopped SQL Server leaves sockets hanging until the OS gives
# up, ~21s on Windows), which is how the service used to wedge. These caps are enforced by
# running the work in a helper thread and abandoning it, so they always apply.
_CONNECT_CAP = float(os.environ.get("DRG_DB_CONNECT_TIMEOUT", "3"))
_CALL_CAP = float(os.environ.get("DRG_DB_CALL_TIMEOUT", "20"))     # > _QUERY_TIMEOUT on purpose
# Revalidate a pooled connection that has been idle this long; a busy pool (burst traffic)
# is used as-is, so the check never shows up on the hot path.
_VALIDATE_IDLE = float(os.environ.get("DRG_DB_VALIDATE_IDLE", "2"))
_VALIDATE_CAP = float(os.environ.get("DRG_DB_VALIDATE_TIMEOUT", "1"))
_pools: dict[str, queue.Queue] = {}
_pools_lock = threading.Lock()

_break_lock = threading.Lock()
_failures = 0
_break_until = 0.0


class DbUnavailable(pymssql.OperationalError):
    """Fast-fail while the breaker is open; mapped to HTTP 503 by app.py."""


def _run_bounded(fn, timeout: float, what: str, on_abandon=None):
    """Run fn() in a helper thread, giving up after `timeout` wall-clock seconds.

    pymssql's own timeouts are DB-Lib level and do not cover a blackholed TCP peer, so this
    is the only bound that actually holds during an outage (docs/35 D13). Abandoned threads
    are daemon threads: they unblock when the socket eventually errors and then exit.
    """
    box: dict = {}
    done = threading.Event()

    def go():
        try:
            box["value"] = fn()
        except BaseException as e:          # noqa: BLE001 - re-raised in the caller
            box["error"] = e
        finally:
            done.set()

    t = threading.Thread(target=go, daemon=True, name=f"db-{what}")
    t.start()
    if not done.wait(timeout):
        if on_abandon is not None:
            on_abandon()
        raise pymssql.OperationalError(
            f"{what} 超过 {timeout:g}s 未返回（连接黑洞或 TDS 未就绪）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _connect(database: str = "drg", cap: float | None = None):
    """Open one connection under a hard wall-clock cap (docs/35 D13)."""
    prof = dict(get_profile("sbo"))
    kwargs = dict(
        server=prof["host"], port=str(prof["port"]), user=prof["user"],
        password=prof["password"], database=database,
        charset=prof.get("charset", "utf8"),
        login_timeout=_LOGIN_TIMEOUT, timeout=_QUERY_TIMEOUT, autocommit=True)
    state = {"abandoned": False}

    def do_connect():
        conn = pymssql.connect(**kwargs)
        if state["abandoned"]:        # we gave up already: never leak the socket
            _close(conn)
            raise pymssql.OperationalError("建连已超时放弃")
        return conn

    return _run_bounded(do_connect, _CONNECT_CAP if cap is None else cap, "建连",
                        on_abandon=lambda: state.update(abandoned=True))


def _new_conn(database: str = "drg"):
    return _connect(database)


def _pool(database: str) -> queue.Queue:
    with _pools_lock:
        q = _pools.get(database)
        if q is None:
            q = _pools[database] = queue.Queue(maxsize=_POOL_SIZE)
        return q


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - already dead is fine
        pass


def _is_alive(conn) -> bool:
    """Bounded liveness probe for a borrowed connection (docs/35 D13).

    Bounded on purpose: against a blackholed peer a plain SELECT 1 hangs exactly like the
    caller's query would, so an unbounded check would become the wedge instead of
    preventing it.
    """
    def probe():
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()

    try:
        _run_bounded(probe, _VALIDATE_CAP, "连接校验")
        return True
    except Exception:                # noqa: BLE001 - any failure means "do not reuse"
        return False


def _acquire(database: str):
    """Take an idle pooled connection, revalidating it when it has been idle a while.

    Creating a fresh connection whenever the queue happened to be empty degraded to one
    connect per request under 20-way concurrency - exactly the cost the pool exists to
    remove (docs/35 D6). Reuse is therefore preferred, but a connection that sat idle
    across an outage must never be handed out (docs/35 D13).
    """
    item = None
    q = _pool(database)
    try:
        item = q.get_nowait()
    except queue.Empty:
        try:
            item = q.get(timeout=_ACQUIRE_TIMEOUT)
        except queue.Empty:
            item = None
    if item is None:
        return _new_conn(database)
    conn, released_at = item
    if time.monotonic() - released_at >= _VALIDATE_IDLE and not _is_alive(conn):
        _close(conn)
        return _new_conn(database)
    return conn


def _release(conn, database: str) -> None:
    try:
        _pool(database).put_nowait((conn, time.monotonic()))
    except queue.Full:               # pool already holds enough idle connections
        _close(conn)


def close_all() -> None:
    """Drop every idle pooled connection, forcing fresh ones (recovery / shutdown / tests)."""
    with _pools_lock:
        pools = list(_pools.values())
    for q in pools:
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            _close(item[0] if isinstance(item, tuple) else item)


def _breaker_open() -> bool:
    return time.monotonic() < _break_until


def _record_failure() -> None:
    global _failures, _break_until
    with _break_lock:
        _failures += 1
        if _failures >= _BREAK_AFTER and not _breaker_open():
            _break_until = time.monotonic() + _BREAK_COOLDOWN


def _record_success() -> None:
    """Clear the breaker, and on the first success after an outage rebuild the pool.

    Every connection opened before the data source went away is dead, so the first sign of
    life is also the right moment to drop them all (docs/35 D13). Doing it here - rather
    than in ping() alone - means recovery works whichever call happens to succeed first,
    and it fires exactly once per outage because the breaker is only open at that point.
    """
    global _failures, _break_until
    with _break_lock:
        was_open = time.monotonic() < _break_until
        _failures = 0
        _break_until = 0.0
    if was_open:
        close_all()


def reset_breaker() -> None:
    """Clear breaker state (tests / after a verified recovery)."""
    _record_success()


def _run(database: str, fn):
    """Run fn(conn), bounded in time, with one retry on a fresh connection.

    Every stage is capped (docs/35 D13): a borrowed connection may already be dead, and a
    dead connection's query can hang. Retrying once covers the ordinary "server closed an
    idle connection" case; the circuit breaker stops retries from multiplying load while
    the data source is down.
    """
    if _breaker_open():
        raise DbUnavailable("数据源不可用（熔断中，快速失败以避免占满工作线程）")
    for attempt in (1, 2):
        try:
            conn = _acquire(database)
        except pymssql.OperationalError:
            _record_failure()
            raise
        try:
            out = _run_bounded(lambda c=conn: fn(c), _CALL_CAP, "数据库调用")
        except pymssql.OperationalError:
            _close(conn)             # dead or hung: never return it to the pool
            if attempt == 2:
                _record_failure()
                raise
            continue
        except Exception:            # noqa: BLE001 - connection itself is still usable
            _release(conn, database)
            raise
        _release(conn, database)
        _record_success()
        return out
    return None


def ping(timeout: float = 2) -> bool:
    """Dependency check for /api/health (docs/35 D7) and the recovery probe (docs/35 D13).

    Uses its own short-lived connection under a wall-clock cap, so a downed - or not yet
    ready - data source cannot make the health endpoint hang. A successful probe closes the
    circuit breaker, which (via _record_success) also rebuilds the connection pool; that is
    what lets the service recover on its own once the database is back, with no restart.
    """
    try:
        conn = _connect("drg", cap=timeout)
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()
    except Exception:  # noqa: BLE001
        return False
    else:
        _record_success()
        return True
    finally:
        _close(conn)


def query(sql: str, params: tuple | None = None, database: str = "drg") -> pd.DataFrame:
    """Parameterized query returning a DataFrame (always bind values; never inline
    text - especially Chinese - into the SQL string)."""
    def fn(conn) -> pd.DataFrame:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql, params or ())
        return pd.DataFrame(cur.fetchall())

    return _run(database, fn)


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-serializable records.

    - NaT/NaN -> null (otherwise FastAPI raises 500 while serializing, e.g. a
      never-logged-in last_login_at);
    - dates -> ISO strings;
    - Decimal -> float (pymssql returns Decimal for decimal columns; to_json would
      emit strings and the frontend could no longer compare/sum numerically).
    """
    d = df.copy()
    for c in d.columns[d.dtypes == object]:
        d[c] = d[c].map(lambda v: float(v) if isinstance(v, Decimal) else v)
    return json.loads(d.to_json(orient="records", force_ascii=False, date_format="iso"))


def month_axis(n: int = 6, today: dt.date | None = None) -> list[str]:
    """The last n calendar months (including the current one), ascending,
    e.g. 2026-04 ... 2026-09."""
    t = today or dt.date.today()
    out = []
    for i in range(n - 1, -1, -1):
        m, y = t.month - i, t.year
        while m <= 0:
            m += 12
            y -= 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def pad_months(rows: list[dict], n: int = 6, key: str = "period") -> list[dict]:
    """Pad monthly rows to "the last n months including the current one"; months
    with no data keep only the period key and None metrics.

    Returning only months that actually have data makes charts look like "there
    were only this many months"; padding makes the gaps explicit on the axis
    (e.g. 2026-06~09 whose settlement returns have not been imported yet).
    """
    have = {str(r.get(key)): r for r in rows}
    return [dict(have[m]) if m in have else {key: m} for m in month_axis(n)]


def execute(sql: str, params: tuple | None = None, database: str = "drg") -> None:
    def fn(conn) -> None:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()

    _run(database, fn)


def audit(phone: str | None, action: str, target: str | None = None,
          detail: str | None = None, ip: str | None = None) -> None:
    """Access audit (docs/29: fills the gap left open in docs/28)."""
    execute("INSERT INTO sys_audit_log (user_phone, action, target, detail, ip) "
            "VALUES (%s, %s, %s, %s, %s)", (phone, action, target, detail, ip))
