"""Create doctor login accounts (sys_user) from the sys_doctor roster with a
uniform initial password.

The hospital decided on "initial password login". The initial password MUST be injected
explicitly via DRG_INIT_PWD: this script creates every account at once, so a built-in
default ("888888") would hand the same weak password to the whole roster - a
horizontal-movement risk (code review S2). Refusing to run without it is deliberate.
Idempotent: existing phone numbers are kept as-is; only new ones are added.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import security  # noqa: E402

INIT_PWD = os.environ.get("DRG_INIT_PWD", "")


def main() -> int:
    if not INIT_PWD:
        print("[!] 未设置 DRG_INIT_PWD：拒绝用统一弱口令批量建号（code review S2）。\n"
              "    请显式注入一次性初始口令后重跑，例如：\n"
              "      PowerShell:  $env:DRG_INIT_PWD = '<一次性强口令>'; "
              "python src/web/backend/seed_users.py\n"
              "    建议随后让本人第一时间改密（首个登录口令仅在使用方内部流转）。")
        return 2
    role = db.query("SELECT role_id FROM sys_role WHERE role_code = 'doctor'")
    if role.empty:
        print("[!] sys_role 缺 doctor 角色，请先应用 src/sql/08_platform.sql")
        return 2
    rid = int(role.iloc[0]["role_id"])

    docs = db.query("SELECT doctor_phone, doctor_name, office FROM sys_doctor WHERE active = 1")
    added = 0
    for r in docs.itertuples(index=False):
        exists = db.query("SELECT 1 AS x FROM sys_user WHERE login_phone = %s", (r.doctor_phone,))
        if exists.empty:
            db.execute(
                "INSERT INTO sys_user (login_phone, user_name, role_id, office, pwd_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (r.doctor_phone, r.doctor_name, rid, r.office, security.hash_pwd(INIT_PWD)))
            added += 1
    total = len(db.query("SELECT 1 AS x FROM sys_user"))
    print(f"新增医师账号 {added} 个（初始密码 {INIT_PWD}）；sys_user 共 {total} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
