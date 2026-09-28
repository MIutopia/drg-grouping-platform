"""Password hashing and login tokens (stdlib only; no bcrypt/jwt dependencies).

Passwords: PBKDF2-HMAC-SHA256, stored as "salt$hash".
Tokens: base64("phone.expiry.signature"), HMAC-signed with DRG_WEB_SECRET.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
import time
from pathlib import Path

# Token signing secret. Resolution order:
#   1) env var DRG_WEB_SECRET (injected explicitly at deploy time, highest priority)
#   2) persisted secret in encrypted storage (src/config/secrets.json, DPAPI/Fernet)
#   3) otherwise **generate randomly and persist** (never fall back to a fixed value:
#      a fixed default key would let anyone with repo access forge login tokens)
# Options 2/3 survive restarts without logging everyone out; rotate via
# `s29_credential.py rotate-web-secret`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "config"))
try:
    import secure as _secure
except Exception:      # noqa: BLE001 - no credential module: fall back to in-process random key
    _secure = None

_secret_env = os.environ.get("DRG_WEB_SECRET")
if _secret_env:
    SECRET = _secret_env.encode()
    _source = "环境变量 DRG_WEB_SECRET"
else:
    persisted = _secure.get_secret("web_secret") if _secure else None
    if persisted:
        SECRET = persisted.encode()
        _source = "加密存储 secrets.json"
    else:
        fresh = secrets.token_urlsafe(48)
        SECRET = fresh.encode()
        if _secure:
            try:
                _secure.set_secret("web_secret", fresh)
                _source = "新生成并已加密持久化"
            except Exception:      # noqa: BLE001 - read-only fs etc: valid for this process only
                _source = "新生成（未能持久化，重启后失效）"
        else:
            _source = "新生成（凭据模块不可用，重启后失效）"
        print(f"[warn] DRG_WEB_SECRET 未设置：{_source}。"
              "生产环境建议显式设置该变量或用 "
              "`python src/etl/s29_credential.py rotate-web-secret` 固化。",
              file=sys.stderr)
ITER = 200_000
TOKEN_TTL = 12 * 3600


def hash_pwd(pwd: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, ITER)
    return f"{salt.hex()}${dk.hex()}"


def verify_pwd(pwd: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, dk_hex = stored.split("$", 1)
    try:
        dk = hashlib.pbkdf2_hmac("sha256", pwd.encode(), bytes.fromhex(salt_hex), ITER)
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def make_token(phone: str) -> str:
    payload = f"{phone}.{int(time.time()) + TOKEN_TTL}"
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()


def read_token(token: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        phone, exp, sig = raw.rsplit(".", 2)
    except Exception:
        return None
    want = hmac.new(SECRET, f"{phone}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, want):
        return None
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return phone
