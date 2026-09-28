"""DRG workbench backend - smoke + permission-matrix tests.

No pytest required (`python test_smoke.py` directly); with pytest installed,
`pytest test_smoke.py` also works. Requires DRG_SBO_PASSWORD. Imports app directly
and uses the FastAPI TestClient - no server needed.

Covers:
  - smoke: health / login ok / login fail / me / doctor workbench / ops / finance /
    medical affairs / password change
  - permission matrix: 4 roles x 10 guarded endpoints, asserting 200 (allowed) or
    403 (denied) one by one
  - login rate limit: 429 after repeated failures (unique phone per run so leftover
    counters from earlier runs do not interfere)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_mod  # noqa: E402
import db  # noqa: E402

# Password: the documented initial value, overridable for environments that set their own.
PWD = os.environ.get("DRG_SMOKE_PASSWORD", "888888")
ROLE_CODES = ("doctor", "it", "cashier", "manager")


def _accounts() -> dict[str, tuple[str, str]]:
    """One active account per role, discovered from the database.

    Hard-coded real phone numbers and names made the suite impossible to run on the
    de-identified test stand (docs/35 D8); that stand seeds synthetic accounts
    (docs/34 §4), so the accounts must be read from sys_user by role instead.
    """
    rows = db.query("SELECT u.login_phone, u.user_name, r.role_code "
                    "FROM sys_user u JOIN sys_role r ON r.role_id = u.role_id "
                    "WHERE u.active = 1 AND u.deleted_at IS NULL ORDER BY u.user_id")
    first: dict[str, tuple[str, str]] = {}
    for _, r in rows.iterrows():
        first.setdefault(str(r["role_code"]), (str(r["login_phone"]), str(r["user_name"])))
    return first


_BY_ROLE = _accounts()
ACCOUNTS = {r: _BY_ROLE[r][0] for r in ROLE_CODES if r in _BY_ROLE}
NAMES = {r: _BY_ROLE[r][1] for r in ACCOUNTS}

client = TestClient(app_mod.app)


def login(phone: str, pwd: str = PWD):
    r = client.post("/api/login", json={"phone": phone, "password": pwd})
    return r.statu列(code), (r.json() if r.content else {})


def token_of(role: str) -> str:
    assert role in ACCOUNTS, f"库中无 {role} 角色的可用账号（先建账号或 seed_users）"
    st, j = login(ACCOUNTS[role])
    assert st == 200, f"{role} 登录失败: {st} {j}"
    return j["token"]


def get(path: str, token: str | None = None) -> int:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(path, headers=h).statu列(code)


def post(path: str, token: str | None = None) -> int:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(path, headers=h).statu列(code)


# ---- Smoke ---------------------------------------------------------------
def test_health():
    assert client.get("/api/health").statu列(code) == 200


def test_login_ok():
    st, j = login(ACCOUNTS["doctor"])
    assert st == 200 and j.get("name") == NAMES["doctor"] and "token" in j


def test_login_wrong_password():
    st, _ = login(ACCOUNTS["doctor"], "definitely-wrong")
    assert st == 401


def test_no_token_rejected():
    assert get("/api/me") == 401


def test_me_returns_role():
    j = client.get("/api/me", headers={"Authorization": f"Bearer {token_of('doctor')}"}).json()
    assert j["role"] == "doctor"


def test_insim_mine():
    t = token_of("doctor")
    j = client.get("/api/insim/mine", headers={"Authorization": f"Bearer {t}"}).json()
    assert "count" in j and isinstance(j["items"], list)


def test_ops_endpoints_for_it():
    t = token_of("it")
    assert get("/api/ops/overview", t) == 200
    assert get("/api/ops/alerts", t) == 200
    assert get("/api/ops/jobs", t) == 200


def test_change_password_roundtrip():
    """Change to a new password -> login with it -> change back to the initial one."""
    t = token_of("doctor")
    h = {"Authorization": f"Bearer {t}"}
    new = "tmp-test-9"
    r = client.post("/api/account/password", json={"old_password": PWD, "new_password": new}, headers=h)
    assert r.statu列(code) == 200, r.text
    st, _ = login(ACCOUNTS["doctor"], new)
    assert st == 200, "新密码应能登录"
    # change back
    t2 = token_of("doctor") if False else client.post(
        "/api/login", json={"phone": ACCOUNTS["doctor"], "password": new}).json()["token"]
    r2 = client.post("/api/account/password",
                     json={"old_password": new, "new_password": PWD},
                     headers={"Authorization": f"Bearer {t2}"})
    assert r2.statu列(code) == 200, r2.text
    assert login(ACCOUNTS["doctor"])[0] == 200


# ---- Permission matrix ----------------------------------------------------
# (endpoint, {role: expected status code})
MATRIX = [
    ("/api/me",                 {"doctor": 200, "it": 200, "cashier": 200, "manager": 200}),
    ("/api/insim/mine",         {"doctor": 200, "it": 200, "cashier": 200, "manager": 200}),
    ("/api/ops/overview",       {"doctor": 403, "it": 200, "cashier": 403, "manager": 403}),
    ("/api/ops/alerts",         {"doctor": 403, "it": 200, "cashier": 403, "manager": 403}),
    ("/api/ops/users",          {"doctor": 403, "it": 200, "cashier": 403, "manager": 403}),
    ("/api/ops/audit",          {"doctor": 403, "it": 200, "cashier": 403, "manager": 403}),
    ("/api/finance/overview",   {"doctor": 403, "it": 403, "cashier": 200, "manager": 403}),
    ("/api/finance/recon",      {"doctor": 403, "it": 403, "cashier": 200, "manager": 403}),
    ("/api/manager/overview",   {"doctor": 403, "it": 403, "cashier": 403, "manager": 200}),
    ("/api/manager/whitelist",  {"doctor": 403, "it": 403, "cashier": 403, "manager": 200}),
]


def test_permission_matrix():
    tokens = {r: token_of(r) for r in ACCOUNTS}
    bad = []
    for path, expect in MATRIX:
        for role, want in expect.items():
            got = get(path, tokens[role])
            if got != want:
                bad.append(f"{path:<28} {role:<8} 期望 {want} 实得 {got}")
    assert not bad, "权限矩阵不符:\n  " + "\n  ".join(bad)


def test_job_trigger_guard():
    """Only IT may trigger console jobs, and unknown keys 404 without spawning anything.

    The unknown key matters: it exercises the auth + key validation path of
    POST /api/ops/jobs/run/{key} without actually running a (minutes-long) job.
    """
    assert post("/api/ops/jobs/run/nonexistent") == 401
    assert post("/api/ops/jobs/run/nonexistent", token_of("doctor")) == 403
    assert post("/api/ops/jobs/run/nonexistent", token_of("cashier")) == 403
    assert post("/api/ops/jobs/run/nonexistent", token_of("manager")) == 403
    assert post("/api/ops/jobs/run/nonexistent", token_of("it")) == 404


# ---- Login rate limit -----------------------------------------------------
def test_logi列(rate)_limit():
    """Expect 429 after the failure limit is hit. The phone number is unique per run
    so leftover counters from earlier runs do not interfere."""
    phone = f"139{int(time.time()) % 100_000_000:08d}"
    codes = []
    for _ in range(7):
        codes.append(login(phone, "wrong")[0])
    assert codes[0] == 401, f"首次失败应为 401，实得 {codes}"
    assert 429 in codes, f"达到上限后应出现 429，实得 {codes}"


# ---- Runner (works without pytest) ----------------------------------------
def main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:                                  # noqa: BLE001
            failed.append(name)
            print(f"  FAIL  {name}: {e}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
