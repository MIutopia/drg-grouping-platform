"""S25 敏感信息扫描：硬编码凭据 + PII（身份证号 / 手机号）。

用途
  - 防止口令/密钥被写进代码或配置（曾经的 `DRG_WEB_SECRET` 默认值即此类问题）；
  - 发现产出物/文档中残留的未脱敏个人信息（本项目「外发必须脱敏」是既定要求）。

用法
    python src/etl/s25_secret_scan.py                 # 扫描代码与文档
    python src/etl/s25_secret_scan.py --out           # 额外扫描 src/out（产出物，量大）
    python src/etl/s25_secret_scan.py --strict        # 有 HIGH 级发现时以退出码 2 结束（可挂 CI）

说明
  - 默认**不扫** `原始资料/`（原始政策与样例文件，本身即含真实数据）与 `src/out/`（生成物，量大需显式 --out）；
  - **测试与种子脚本属夹具豁免**：`test_*` / `seed_users.py` 里的账号与口令是测试夹具，
    若一并报警会淹没真问题，故豁免并单独计数（见报告"豁免"项）；
  - 明显为占位的值（`changeme` / `<...>` / `{{...}}` / `example` …）不计；
  - 报告写到 `src/out/security/s25_secret_scan.md`。

也可在信息科控制台「作业触发」页手动触发（key=s25）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pii_mask

ROOT = Path(__file__).resolve().parents[2]          # E:\DRG
OUT_DIR = ROOT / "项目" / "out" / "security"

SCAN_SUFFIX = {".py", ".js", ".vue", ".sql", ".md", ".json", ".csv", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {"node_modules", "dist", "__pycache__", ".git", ".vite", "原始资料"}

# 形态上明显是占位/示例的值，不算敏感
PLACEHOLDER = re.compile(
    r"(?i)(change|changeme|your[-_ ]|<[^>]*>|%s|\{\{|\*+|example|placeholder|xxx+|todo|dummy)")

# 测试夹具与种子脚本：其中的账号/口令是测试数据，豁免并单独计数
# pii_mask 本身需以真实格式举例说明掩码规则，故一并豁免
SAFE_PATH = re.compile(r"(?i)(test_|seed_users|conftest|pii_mask)")


def _cred_patterns() -> list[tuple[str, str]]:
    return [
        (r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{4,})['\"]", "口令"),
        (r"(?i)\b(?:secret|token|api_?key|access_?key)\s*[:=]\s*['\"]([^'\"]{6,})['\"]", "密钥/令牌"),
    ]


def _pii_patterns() -> list[tuple[str, str]]:
    return [
        (r"\b1[3-9]\d{9}\b", "手机号"),
        (r"\b\d{17}[\dXx]\b", "身份证号"),
    ]


def iter_files(include_out: bool):
    roots = [ROOT / "项目" / d for d in ("etl", "web", "config", "sql", "run")]
    roots.append(ROOT / "docs")
    if include_out:
        roots.append(ROOT / "项目" / "out")
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in SCAN_SUFFIX:
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            yield p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", action="store_true", help="额外扫描 src/out 产出物")
    ap.add_argument("--strict", action="store_true", help="存在 HIGH 级发现时退出码 2")
    args = ap.parse_args()

    findings: list[dict] = []
    exempt = 0
    for f in iter_files(args.out):
        rel = str(f.relative_to(ROOT))
        is_fixture = bool(SAFE_PATH.search(f.name))
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:                                        # noqa: BLE001
            continue
        for pats, level in ((_cred_patterns(), "HIGH"), (_pii_patterns(), "MEDIUM")):
            for pat, kind in pats:
                for m in re.finditer(pat, text):
                    val = m.group(1) if m.groups() else m.group(0)
                    if level == "HIGH" and PLACEHOLDER.search(val):
                        continue
                    if is_fixture:
                        exempt += 1
                        continue
                    line = text[:m.start()].count("\n") + 1
                    # 报告里绝不落原文——否则扫描器自己就成了 PII 泄漏源（曾一次性泄漏 80 处）
                    if level == "HIGH":
                        disp = val[:2] + "***" + f"(len={len(val)})"
                    else:
                        disp = pii_mask.mask_text(val[:24])[0]
                    findings.append({"level": level, "kind": kind, "file": rel, "line": line,
                                     "hit": disp})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    high = [x for x in findings if x["level"] == "HIGH"]
    med = [x for x in findings if x["level"] == "MEDIUM"]
    lines = ["# S25 敏感信息扫描", "",
             f"- HIGH（硬编码凭据）：**{len(high)}** 条",
             f"- MEDIUM（PII 形态命中）：**{len(med)}** 条",
             f"- 豁免（测试夹具/种子脚本）：**{exempt}** 处",
             "",
             "> 注意：**默认不扫** `原始资料/`（原始政策与样例件）与 `src/out/`（生成物，量大）。",
             "> `src/out/` 已被 .gitignore 排除、**未入库**，但实测其中含真实患者身份证号等 PII，",
             "> **外发/共享前必须脱敏**；需盘点产出物时用 `python src/etl/s25_secret_scan.py --out`。",
             ""]
    for grp, title in ((high, "## HIGH · 硬编码凭据"), (med, "## MEDIUM · PII 形态命中")):
        lines += [title, "", "| 级别 | 类型 | 文件 | 行 | 命中 |", "|---|---|---|---:|---|"]
        if grp:
            for x in grp[:80]:
                lines.append(f"| {x['level']} | {x['kind']} | `{x['file']}` | {x['line']} | `{x['hit']}` |")
        else:
            lines.append("| — | 无 | — | — | — |")
        lines.append("")
    (OUT_DIR / "s25_secret_scan.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"HIGH {len(high)} 条 | MEDIUM {len(med)} 条 | 豁免(测试夹具) {exempt} 处")
    print(f"报告 -> {OUT_DIR / 's25_secret_scan.md'}")
    for x in high[:10]:
        print(f"  [HIGH] {x['kind']} {x['file']}:{x['line']}")
    if args.strict and high:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
