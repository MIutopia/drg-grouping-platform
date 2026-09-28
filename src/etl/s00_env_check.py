"""S00 Pre-flight environment check.

Verifies read-only connectivity, the active driver and the key source objects
required by phase 1, then writes a report used as the project kick-off audit trail.
Docs: docs/15-代码模块说明与作业手册.md §6.1.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import srcschema  # noqa: E402  源库表名逻辑映射
from dbio import backend_of, list_tables, query_df, row_counts, server_info  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR, describe  # noqa: E402

# Key source objects required by phase 1 (see docs/04, docs/05).
KEY_TABLES = [
    "源表(admission)",                    # medical record header, one row per admission
    "源表(diagnosis)",           # diagnosis detail (列(kind_input) / 列(kind_diagnose) / 列(order))
    "源表(operation)",          # operation detail (one-to-many)
    "源表(case_out_dx)",      # discharge diagnosis detail
    "源表(icd_dict)",              # in-house master code table (incl. TCM disease/syndrome)
    "源表(vendor_groups)",             # vendor grouping dictionary, 880 groups (reference only)
    "源表(vendor_cc)",                 # vendor CC table
    "源表(vendor_mcc)",                # vendor MCC table
    "源表(vendor_pre_grouping)",       # vendor pre-grouping results (since 2023-05)
    "源表(vendor_org)",         # vendor organization parameters (rate is stale, see docs/05)
    "源表(settle_bill)",  # medicare settlement detail
    "源表(settle)",       # medicare interaction log
]

CHECK_TABLES = ["源表(源表(病案main))", "源表(源表(病案dx))", "源表(fee_check_raw)",
                "源表(fee_treat_raw)", "源表(fee_drug_raw)"]


def main() -> int:
    lines: list[str] = []
    ok = True

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    emit(f"# S00 环境体检报告")
    emit()
    emit(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    emit(f"- 目标：{describe('sbo')}")
    emit()

    # 1. connectivity and version
    emit("## 1. 源库连接与版本")
    try:
        info = server_info("sbo")
        ok_backend = backend_of("sbo")
        emit(f"- 驱动后端：**{ok_backend}**")
        emit(f"- 登录名：`{info.get('login_name')}`")
        emit(f"- 当前库：`{info.get('current_db')}`")
        emit(f"- 版本级别：{info.get('sp_level')} / {info.get('edition')}")
        ver = str(info.get("version", "")).split("\n")[0]
        emit(f"- 版本串：{ver}")
        emit(f"- 服务器时间：{info.get('server_time')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        emit(f"- **连接失败**：`{type(exc).__name__}: {exc}`")
        _write(lines)
        return 1
    emit()

    # 2. key source objects
    emit("## 2. 一期依赖的关键源对象")
    tbl_df = list_tables("%", "sbo")
    present = set(tbl_df["TABLE_NAME"].str.upper())
    # KEY_TABLES 写的是逻辑令牌（源表(key)）；库返回的是物理表名，比较前必须展开，
    # 否则永远报"缺失"（令牌与物理名不相等）。
    phys_of = {t: srcschema.expand(t) for t in KEY_TABLES}
    missing = [t for t, phys in phys_of.items() if phys.upper() not in present]

    if missing:
        ok = False
        emit(f"- **缺失 {len(missing)} 个关键对象**：{', '.join(missing)}")
    else:
        emit(f"- 关键对象 {len(KEY_TABLES)} 个全部在位")

    inplace = [p for p in phys_of.values() if p.upper() in present]
    counts = row_counts(inplace, "sbo")
    # 报告里仍显示逻辑令牌（与 notes 的键一致），不暴露现场物理表名
    back = {p: t for t, p in phys_of.items()}
    counts["table"] = counts["table"].map(lambda v: back.get(v, v))
    emit()
    emit("| 源对象 | 行数 | 说明 |")
    emit("|---|---:|---|")
    notes = {
        "源表(admission)": "病案首页，一患一行",
        "源表(diagnosis)": "诊断明细，列(order) 定主次",
        "源表(operation)": "手术操作明细",
        "源表(case_out_dx)": "出院诊断明细",
        "源表(icd_dict)": "院内总码表（B 中医病 / Z 证型）",
        "源表(vendor_groups)": "既有系统分组字典 880 组（参考）",
        "源表(vendor_cc)": "既有系统 CC 表（参考）",
        "源表(vendor_mcc)": "既有系统 MCC 表（参考）",
        "源表(vendor_pre_grouping)": "既有预分组结果",
        "源表(vendor_org)": "既有系统机构参数（费率过时）",
        "源表(settle_bill)": "医保结算明细，基金/自付拆分源",
        "源表(settle)": "医保交互流水",
    }
    for _, r in counts.iterrows():
        n = f"{r['rows']:,}" if r["rows"] >= 0 else f"ERR({r.get('error', '')})"
        emit(f"| {r['table']} | {n} | {notes.get(r['table'], '')} |")
    emit()

    # 3. candidate table discovery by keyword
    emit("## 3. 候选对象探测（表名关键词）")
    for kw in ("Fee", "Invoice", "Man", "Drg", "Icd", "Basic"):
        sub = tbl_df[tbl_df["TABLE_NAME"].str.contains(kw, case=False, na=False)]
        emit(f"- `{kw}` 命中 {len(sub)} 张")
    emit()
    emit("<details><summary>Fee / Invoice 相关表清单</summary>")
    emit()
    fee = tbl_df[tbl_df["TABLE_NAME"].str.contains("Fee|Invoice", case=False, na=False)]
    for t in fee["TABLE_NAME"]:
        emit(f"- {t}")
    emit()
    emit("</details>")
    emit()

    # 4. local settlement-return samples
    emit("## 4. 结算返回 Excel（本地样本）")
    try:
        from settings import RAW_DIR  # noqa: PLC0415
        d = RAW_DIR / "DRG结算样表"
        files = sorted(d.glob("*.xlsx"))
        if files:
            for f in files:
                emit(f"- `{f.name}` — {f.stat().st_size / 1024:,.0f} KB")
        else:
            ok = False
            emit("- **未找到 xlsx 样本**")
    except Exception as exc:  # noqa: BLE001
        ok = False
        emit(f"- 检查失败：{exc}")
    emit()
    # 5. 源库表名抽象自检（代码里不得再出现物理表名）
    emit("## 5. 源库表名抽象自检")
    try:
        # 注意：srcschema 已在模块顶部导入。此处**不得**再写局部 import —— 那会让它在
        # main() 内成为局部名，导致本节之前的用法抛 UnboundLocalError 并中断整个作业。
        bad = srcschema.audit_sources()
        if bad:
            ok = False
            emit(f"- **{len(bad)} 处代码直接写了物理表名**（应写 `源表(key)`）：")
            for b in bad[:10]:
                emit(f"  - {b}")
        else:
            emit("- 通过：作业代码只写 `源表(key)` / `列(key)`；物理名集中在 "
                 f"`src/config/source_schema.json`（{len(srcschema.keys())} 个表键）与 "
                 f"`src/config/source_columns.json`（{len(srcschema.col_keys())} 个列键）")
    except Exception as exc:  # noqa: BLE001
        ok = False
        emit(f"- 检查失败：{exc}")
    emit()

    emit(f"## 结论：{'通过，可开工' if ok else '存在阻断项，需先处理'}")

    _write(lines)
    print(f"\n报告已写入: {OUT_DIR / 's00_env_check.md'}")
    return 0 if ok else 2


def _write(lines: list[str]) -> None:
    (OUT_DIR / "s00_env_check.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
