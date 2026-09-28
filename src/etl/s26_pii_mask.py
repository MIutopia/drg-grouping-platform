"""S26 产出物脱敏副本：把 `src/out` 生成脱敏副本到 `src/out/masked`。

**不改动原文件**——原文件是 ETL 与作业的输入（例如 `settlement_returns.csv` 供 s01/s10/s11，
`insim_*.csv` 供 s18 --load，其中的医师手机号更是医师工作台的登录映射键），就地脱敏会破坏管线。
需要**外发/共享/给人看**时，使用 `src/out/masked/` 下的副本。

用法
    python src/etl/s26_pii_mask.py                 # 默认 out -> out/masked
    python src/etl/s26_pii_mask.py --src src/out --dst src/out/masked
    python src/etl/s26_pii_mask.py --inplace        # 就地脱敏（仅限报告类目录，会告警）

产物
    - `src/out/masked/` 下的脱敏副本（保持相对目录结构）
    - `src/out/security/s26_mask_report.md` 统计报告

也可在信息科控制台「作业触发」页手动触发（key=s26）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pii_mask  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]          # E:\DRG
OUT_DIR = ROOT / "项目" / "out"
SEC_DIR = OUT_DIR / "security"

TEXT_SUFFIX = {".md", ".csv", ".txt", ".json", ".yml", ".yaml"}
SKIP_DIRS = {"masked", "node_modules", "__pycache__", ".vite"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(OUT_DIR), help="源目录（默认 src/out）")
    ap.add_argument("--dst", default=str(OUT_DIR / "masked"), help="脱敏副本目录")
    ap.add_argument("--inplace", action="store_true",
                    help="就地脱敏（危险：仅限报告类文件；默认只出不改）")

    args = ap.parse_args()
    src, dst = Path(args.src), Path(args.dst)
    if not src.exists():
        print(f"[!] 源目录不存在: {src}")
        return 2

    if args.inplace:
        print("[warn] --inplace：将直接改写原文件，确认这些文件不被 ETL 消费！")
        dst = src

    SEC_DIR.mkdir(parents=True, exist_ok=True)
    files, hits, copied = 0, 0, 0
    details: list[str] = []

    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        if any(p in SKIP_DIRS for p in f.parts):
            continue
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        if f.suffix.lower() in TEXT_SUFFIX:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:                                    # noqa: BLE001
                continue
            masked, n = pii_mask.mask_text(text)
            files += 1
            if n:
                hits += n
                details.append(f"| `{rel}` | {n} |")
            if args.inplace:
                if n:
                    f.write_text(masked, encoding="utf-8")
                    copied += 1
            else:
                target.write_text(masked, encoding="utf-8")
                copied += 1
        elif not args.inplace:
            shutil.copy2(f, target)                              # 非文本原样拷贝
            copied += 1

    lines = ["# S26 产出物脱敏", "",
             f"- 源：`{src}`",
             f"- 目标：`{dst}`（{'就地' if args.inplace else '副本'}）",
             f"- 扫描文本文件 **{files}** 个，脱敏 **{hits}** 处，写入 **{copied}** 个文件", "",
             "## 含 PII 的文件", "", "| 文件 | 脱敏处数 |", "|---|---:|"]
    lines += (details[:60] or ["| — | 0 |"])
    lines += ["", "> 说明：原文件保留未改（供 ETL 消费）；外发/共享请使用 `src/out/masked/` 下的副本。", ""]
    (SEC_DIR / "s26_mask_report.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"文本 {files} 个 | 脱敏 {hits} 处 | 写入 {copied} 个 -> {dst}")
    print(f"报告 -> {SEC_DIR / 's26_mask_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
