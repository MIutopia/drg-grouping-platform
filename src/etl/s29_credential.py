"""S29 凭据管理：把配置中的明文口令改为密文存储。

背景（docs/28 安全角色 75% 的剩余缺口）：`src/config/local.json` 与
`metabase.json` 里的数据库口令、API Key 一直是**明文**。两文件虽已 gitignore，
但"文件泄露 = 凭据泄露"。本作业改为密文存储（Windows DPAPI 优先，见
`src/config/secure.py` 的选型说明）。

用法
----
    python src/etl/s29_credential.py status               # 哪些口令仍是明文
    python src/etl/s29_credential.py protect [--dry]       # 明文 -> 密文（原地改写）
    python src/etl/s29_credential.py set <档案>            # 重设口令（交互式不回显）
    python src/etl/s29_credential.py rotate-web-secret     # 轮换平台令牌密钥
    python src/etl/s29_credential.py init-key              # 非 Windows 时生成 Fernet 密钥

注意
----
- DPAPI 密文**绑定当前 Windows 用户 + 机器**：换机/换用户需重新执行 `set` 录入口令，
  这是"密钥不落盘"的代价，也是它能抵御"文件被拷走"的原因。
- `protect` 会先写 `.bak` 备份，成功后再删除；失败时可用备份还原。
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
import secure  # noqa: E402
from settings import CONFIG_DIR  # noqa: E402

TARGETS = ("local.json", "metabase.json")
SECRET_KEYS = ("password", "api_key", "apikey", "key", "token", "secret", "pwd")


# ------------------------------------------------------------------
# 遍历
# ------------------------------------------------------------------
def walk(node, path="", hits=None):
    """收集所有疑似凭据字段的 (路径, 值)。"""
    if hits is None:
        hits = []
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, f"{path}.{k}" if path else k, hits)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]", hits)
    elif isinstance(node, str) and node:
        leaf = path.split(".")[-1].lower()
        if any(s == leaf or leaf.endswith("_" + s) or leaf.endswith(s) for s in SECRET_KEYS):
            hits.append((path, node))
    return hits


def set_by_path(root, dotted, value):
    cur = root
    parts = dotted.replace("[", ".").replace("]", "").split(".")
    for p in parts[:-1]:
        cur = cur[int(p)] if p.isdigit() and isinstance(cur, list) else cur[p]
    last = parts[-1]
    if last.isdigit() and isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


# ------------------------------------------------------------------
# 子命令
# ------------------------------------------------------------------
def cmd_status() -> int:
    print("加密后端:", secure.backend_name())
    print("平台密钥(secrets.json):", secure.secret_status() or "（无）")
    print()
    any_plain = False
    for name in TARGETS:
        p = CONFIG_DIR / name
        if not p.exists():
            print(f"- {name}: 不存在，跳过")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"- {name}: 解析失败 {exc}")
            continue
        hits = walk(data)
        if not hits:
            print(f"- {name}: 未发现凭据字段")
            continue
        print(f"- {name}:")
        for path, val in hits:
            state = "密文 [OK]" if secure.is_protected(val) else "明文 [WARN]"
            if not secure.is_protected(val):
                any_plain = True
            print(f"    {path:<40} {state}")
    print()
    if any_plain:
        print("-> 仍有明文口令，运行 `python src/etl/s29_credential.py protect` 转为密文。")
    else:
        print("-> 全部凭据已为密文。")
    return 0


def cmd_protect(dry: bool) -> int:
    changed = 0
    for name in TARGETS:
        p = CONFIG_DIR / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: 解析失败，跳过（{exc}）")
            continue
        hits = [(path, val) for path, val in walk(data) if not secure.is_protected(val)]
        if not hits:
            print(f"{name}: 无需处理")
            continue
        for path, val in hits:
            set_by_path(data, path, secure.protect(val))
            changed += 1
            print(f"{name}: {path} -> 已加密")
        if not dry:
            bak = p.with_suffix(p.suffix + ".bak")
            bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            bak.unlink()          # 写回成功即删除备份
    print()
    print(f"共处理 {changed} 项" + ("（dry-run，未写入）" if dry else "，已写入"))
    return 0


def cmd_set(profile: str) -> int:
    p = CONFIG_DIR / "local.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if profile not in data:
        print(f"local.json 中没有档案 {profile}；现有：{sorted(k for k in data if not k.startswith('_'))}")
        return 1
    v1 = getpass.getpass(f"{profile} 新口令（不回显）: ")
    v2 = getpass.getpass("再输入一次: ")
    if not v1:
        print("口令为空，已取消")
        return 1
    if v1 != v2:
        print("两次输入不一致，已取消")
        return 1
    data[profile]["password"] = secure.protect(v1)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{profile} 口令已以密文写入（后端：{secure.backend_name()}）")
    return 0


def cmd_rotate_web_secret() -> int:
    new = secrets.token_urlsafe(48)
    secure.set_secret("web_secret", new)
    print("平台令牌密钥已轮换并加密保存。")
    print("后果：所有已登录用户需重新登录（原令牌立即失效）。")
    return 0


def cmd_init_key() -> int:
    if secure._dpapi_available():
        print("当前为 Windows，优先使用 DPAPI，无需密钥文件。")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    secure.KEY_FILE.write_text(key, encoding="utf-8")
    try:
        secure.KEY_FILE.chmod(0o600)
    except Exception:  # noqa: BLE001 - Windows 上 chmod 支持有限
        pass
    print(f"已生成 {secure.KEY_FILE}（权限 600）。")
    print("提示：密钥文件与密文同目录只能防「误提交」，防不住「目录被拿走」；"
          "Windows 上请优先用 DPAPI。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="S29 凭据管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="查看哪些口令仍是明文")
    p = sub.add_parser("protect", help="明文口令转为密文")
    p.add_argument("--dry", action="store_true")
    p = sub.add_parser("set", help="重设某个连接档案的口令")
    p.add_argument("profile")
    sub.add_parser("rotate-web-secret", help="轮换平台令牌密钥")
    sub.add_parser("init-key", help="生成 Fernet 密钥（非 Windows 回落用）")
    args = ap.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "protect":
        return cmd_protect(args.dry)
    if args.cmd == "set":
        return cmd_set(args.profile)
    if args.cmd == "rotate-web-secret":
        return cmd_rotate_web_secret()
    return cmd_init_key()


if __name__ == "__main__":
    raise SystemExit(main())
