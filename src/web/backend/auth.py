"""Login state and role-permission dependencies (shared by app.py and ops_api.py)."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

import db
import security


def current_user(authorization: str = Header(default="")) -> dict:
    """Resolve a Bearer token into a sys_user row (with role)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    phone = security.read_token(authorization[7:])
    if not phone:
        raise HTTPException(401, "登录已失效，请重新登录")
    u = db.query(
        "SELECT u.login_phone, u.user_name, u.office, r.role_code, r.role_name "
        "FROM sys_user u JOIN sys_role r ON r.role_id = u.role_id "
        "WHERE u.login_phone = %s AND u.active = 1", (phone,))
    if u.empty:
        raise HTTPException(401, "账号不存在或已停用")
    return u.iloc[0].to_dict()


def require_roles(*codes: str):
    """Build a dependency that allows only the given roles."""
    def dep(user: dict = Depends(current_user)) -> dict:
        if user["role_code"] not in codes:
            raise HTTPException(403, "无权访问该功能")
        return user
    return dep
