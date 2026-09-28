"""凭据保护：把配置里的口令由明文改为密文存储。

## 为什么优先用 Windows DPAPI，而不是"密钥文件"

一个常见的假安全做法：生成一个密钥文件，放在密文旁边。但对"有人拿到了
`src/config/` 目录"这个真实威胁模型而言，这几乎无效——攻击者会**同时拿到
密文和密钥**。

DPAPI（`CryptProtectData`）的密钥由 Windows 按**用户 + 机器**保管，不落盘：

- 密文文件单独拷走，在别的机器/别的用户下**解不开**；
- 不需要保管任何密钥文件，也就没有"密钥泄露"这一环；
- 零第三方依赖（`ctypes` 调系统 API）。

代价是密文**不跨机器/用户移植**（换机需重新录入一次口令），本项目开发机与
服务器同为 Windows，可接受；非 Windows 时自动回落到 Fernet
（密钥取 `DRG_CONFIG_KEY` 环境变量或 `.configkey` 文件）。

## 用法

密文形如 `dpapi1:<base64>` / `fernet1:<base64>`，可直接放进 `local.json` 的
`password` 字段，`settings.get_profile()` 会自动识别并解密；仍兼容明文
（读到明文时打印告警，提示运行 `s29_credential.py protect`）。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent
SECRETS_FILE = CONFIG_DIR / "secrets.json"      # 平台自身密钥（如 web 令牌密钥）
KEY_FILE = CONFIG_DIR / ".configkey"            # Fernet 回落用，严禁入库

DPAPI_PREFIX = "dpapi1:"
FERNET_PREFIX = "fernet1:"
# 误把这些字段当成口令加密会破坏配置，故只加密明确清单里的字段
SECRET_FIELDS = ("password", "api_key", "key", "token", "secret", "pwd")


# ------------------------------------------------------------------
# Windows DPAPI
# ------------------------------------------------------------------
def _dpapi_available() -> bool:
    return sys.platform == "win32"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(raw: bytes) -> bytes:
    buf = ctypes.create_string_buffer(raw)
    inp = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob)
    inp = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise RuntimeError("CryptUnprotectData 调用失败（密文可能来自其他机器/用户，"
                           "DPAPI 密文不可跨机迁移，请重新录入口令）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


# ------------------------------------------------------------------
# Fernet 回落（非 Windows，或需要跨机迁移时）
# ------------------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet

    key = os.environ.get("DRG_CONFIG_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise RuntimeError(
            "非 Windows 环境且未提供 Fernet 密钥：请设置环境变量 DRG_CONFIG_KEY，"
            f"或运行 `python src/etl/s29_credential.py init-key` 生成 {KEY_FILE.name}")
    return Fernet(key.encode())


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------
def is_protected(value: object) -> bool:
    return isinstance(value, str) and (value.startswith(DPAPI_PREFIX)
                                       or value.startswith(FERNET_PREFIX))


def protect(plain: str) -> str:
    """明文 -> 密文（带前缀标记）。"""
    raw = str(plain).encode("utf-8")
    if _dpapi_available():
        try:
            return DPAPI_PREFIX + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
        except Exception:      # noqa: BLE001 - DPAPI 不可用时静默回落
            pass
    return FERNET_PREFIX + _fernet().encrypt(raw).decode("ascii")


def unprotect(token: str) -> str:
    """密文 -> 明文。明文输入按原样返回（由调用方决定是否告警）。"""
    if token is None:
        return ""
    if not is_protected(token):
        return str(token)
    if token.startswith(DPAPI_PREFIX):
        return _dpapi_unprotect(base64.b64decode(token[len(DPAPI_PREFIX):])).decode("utf-8")
    return _fernet().decrypt(token[len(FERNET_PREFIX):].encode("ascii")).decode("utf-8")


def backend_name() -> str:
    return "DPAPI(用户/机器绑定)" if _dpapi_available() else "Fernet(DRG_CONFIG_KEY/.configkey)"


# ------------------------------------------------------------------
# 平台自身密钥存储（web 令牌密钥等）
# ------------------------------------------------------------------
def _load_secrets() -> dict:
    if not SECRETS_FILE.exists():
        return {}
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception:          # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _save_secrets(data: dict) -> None:
    SECRETS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def get_secret(name: str, default: str | None = None) -> str | None:
    """读取平台密钥；缺失返回 default。"""
    v = _load_secrets().get(name)
    if not v:
        return default
    try:
        return unprotect(v)
    except Exception:          # noqa: BLE001
        return default


def set_secret(name: str, value: str) -> None:
    """写入平台密钥（自动加密）。"""
    data = _load_secrets()
    data[name] = protect(value)
    _save_secrets(data)


def secret_status() -> dict:
    data = _load_secrets()
    return {k: ("已加密" if is_protected(v) else "明文(!)") for k, v in data.items()}
