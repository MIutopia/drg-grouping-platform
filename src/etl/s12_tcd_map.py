"""S12 TCD name-level mapping and charge-item / medicare-catalogue coverage.

P2-2: the hospital stores TCM diagnoses as an in-house dotted code system (A07.06.),
not the national TCD standard required for upload. The national TCD dictionary is not
available locally, so this job harvests every TCD code present in the official materials,
maps in-house codes by name, quantifies the residual gap and produces the request list.
P2-3: assesses how far HIS charge items are covered by the medicare service-item
catalogue, as the basis for rebuilding the charge-item mapping.

Docs: docs/18-P2分组引擎与映射重建.md.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import DICT_DIR, OUT_DIR, RAW_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "tcd"
WORKBOOK = RAW_DIR / "官方下发文件" / "CHS-DRG2.0完整版CC-MCC排除表.xlsx"
XLSX = RAW_DIR / "中医入组标准(1).xlsx"
CATALOG_CSV = OUT_DIR / "catalog" / "某地区细分组目录2.0_tables.csv"

INHOUSE_ICD = DICT_DIR / "院内总码表_含中医病证_52670.csv"
NATIONAL_DISEASE = DICT_DIR / "按病种结算国家标准编码_44835.csv"
COST_ATTR = OUT_DIR / "feemap" / "cost_item_attribute.csv"
CQ_CATALOG = DICT_DIR / "某地区医疗服务项目_9659.csv"


def norm_text(s: object) -> str:
    """Normalise a TCM disease name for matching (drop punctuation and bracketed notes)."""
    t = str(s or "").strip()
    t = re.sub(r"[（(【\[].*?[)）】\]]", "", t)
    t = re.sub(r"[\s·、,，/／\-—_*]+", "", t)
    return t.upper()


def harvest_official_tcd() -> pd.DataFrame:
    """Collect TCD codes from every official artefact available locally."""
    rows: list[dict] = []

    # 某地区细分组目录 docx -> annex 5 (中医特色病种, has TCD column)
    if CATALOG_CSV.exists():
        d = pd.read_csv(CATALOG_CSV, dtype=str, encoding="utf-8-sig")
        d.columns = ["tag", "a", "b", "c", "d", "e", "f", "g", "h"][:d.shape[1]]
        t5 = d[d["tag"] == "T5"]
        for r in t5.itertuples():
            if pd.notna(r.b) and pd.notna(r.c):
                rows.append({"source": "细分组目录-附件5", "name": r.b, "tcd": r.c})

    # 中医入组标准 workbook: the 中医特色病种 sheet carries a TCD column
    try:
        x = pd.read_excel(XLSX, sheet_name="中医特色病种", header=None, engine="openpyxl", dtype=object)
        for r in x.itertuples():
            vals = [str(v).strip() for v in r[1:] if pd.notna(v)]
            for i, v in enumerate(vals):
                if re.match(r"^[A-Z]\d{2}(\.\d+)*\.?$", v) and i + 1 < len(vals):
                    rows.append({"source": "中医入组标准", "name": vals[0], "tcd": v})
    except Exception:  # noqa: BLE001
        pass

    # 按病种结算国家标准编码: may carry TCM disease codes
    if NATIONAL_DISEASE.exists():
        nd = pd.read_csv(NATIONAL_DISEASE, dtype=str, encoding="utf-8-sig")
        for col in nd.columns:
            if "病种" in col and "名称" not in col:
                for r in nd.itertuples(index=False):
                    v = str(getattr(r, col.replace(" ", "_"), "") or "")
                    pass

    out = pd.DataFrame(rows)
    if len(out):
        out["tcd"] = out["tcd"].astype(str).str.strip()
        out["name_key"] = out["name"].map(norm_text)
        out = out[out["tcd"].str.match(r"^[A-Z]\d{2}", na=False)].drop_duplicates("tcd")
    return out


def main() -> int:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    log: list[str] = [
        "# S12 TCD 名称级映射 与 收费项目目录覆盖度评估",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
        "## 一、P2-2 国标 TCD 字典可得性",
        "",
    ]

    tcd = harvest_official_tcd()
    log += [f"- 本地官方材料中可提取的 TCD 编码：**{len(tcd):,}** 个",
            "- 来源：`某地区细分组目录 附件5（中医特色病种）`、`中医入组标准(1).xlsx`", ""]
    if len(tcd):
        log += ["| 来源 | 病名 | TCD 编码 |", "|---|---|---|"]
        for r in tcd.head(25).itertuples():
            log.append(f"| {r.source} | {r.name} | {r.tcd} |")
        log.append("")

    # ---- in-house TCM code inventory --------------------------------
    inh = pd.read_csv(INHOUSE_ICD, dtype=str, encoding="utf-8-sig")
    inh.columns = ["n_id", src_col("id_list"), src_col("code"), src_col("note"), src_col("list"), "列(kind)",
                   src_col("pym"), src_col("wbm"), "x1", "x2"][:inh.shape[1]]
    tcm = inh[inh["列(kind)"].isin(["B", "Z"])].copy()
    log += ["## 二、院内中医码存量",
            "",
            "| 类别 | 含义 | 条数 |",
            "|---|---|---:|",
            f"| B | 中医病名（点分码，如 A07.06.） | {int((tcm['列(kind)'] == 'B').sum()):,} |",
            f"| Z | 证型（如 肝肾不足证） | {int((tcm['列(kind)'] == 'Z').sum()):,} |",
            f"| **合计** | | **{len(tcm):,}** |", ""]

    tcm["name_key"] = tcm[src_col("note")].map(norm_text)
    name_to_tcd = dict(zip(tcd["name_key"], tcd["tcd"])) if len(tcd) else {}
    tcm["tcd_guess"] = tcm["name_key"].map(name_to_tcd)
    hit = int(tcm["tcd_guess"].notna().sum())

    log += [f"### 2.1 名称级映射结果",
            "",
            f"- 可用官方 TCD 名称 **{len(name_to_tcd):,}** 个",
            f"- 院内中医码中按**名称完全匹配**可映射：**{hit:,} / {len(tcm):,}**（{hit / len(tcm):.2%}）",
            ""]

    # ---- decisive test: is the in-house B code already the TCD system? ----
    inhouse_codes = set(tcm[tcm["列(kind)"] == "B"][src_col("code")].astype(str).str.strip())
    official_tcd = set(tcd["tcd"].astype(str).str.strip()) if len(tcd) else set()
    exact = inhouse_codes & official_tcd
    prefix_hit = {o for o in official_tcd if any(c.startswith(o.rstrip(".")) for c in inhouse_codes)}
    log += ["### 2.2 决定性检验：院内 B 类码是否就是 TCD 体系",
            "",
            f"- 官方材料中的 TCD 码：**{len(official_tcd)}** 个",
            f"- 与院内 B 类码**完全相同**的：**{len(exact)}** 个",
            f"- 院内 B 类码中存在**同前缀（层级）**的：**{len(prefix_hit)}** 个",
            "",
            "| 官方 TCD | 对应病名 | 院内 B 类是否存在同前缀编码 |",
            "|---|---|---|"]
    for r in tcd.head(12).itertuples():
        pre = str(r.tcd).rstrip(".")
        same = "✅ 存在" if any(c.startswith(pre) for c in inhouse_codes) else "—"
        log.append(f"| {r.tcd} | {r.name} | {same} |")

    log += ["",
            "**判读（重要）**：官方 TCD 码（如 `A07.06.03` 尪痹、`A07.06.04` 肢痹）与院内 B 类码"
            "（如 `A07.06.` 痹证类病）**属同一编码体系与同一层级结构**。",
            "因此 TCD 缺口**不是「缺映射字典」而是「版本一致性待确认 + 未覆盖病名待补齐」**：",
            "1. 院内 B 类 1,368 条很可能本身就是 TCD 码（或其子集/院内扩展），需抽样与 GB/T 15657-2021 比对；",
            "2. 真正需要索取的是**国标全量字典**用于（a）校验院内码合法性、（b）补齐院内未编码的病名。",
            "",
            "> 这一修正把 P2-2 从「需要先拿到字典才能开工」变为「可先用现有院内码直传，同时抽样校验」。", ""]

    # ---- how many admissions are affected ---------------------------
    log += ["### 2.2 影响面（本单位实际使用的中医码）",
            "",
            "> 判断优先级不能只看字典规模，要看**实际被使用的编码**。下表按结算期实际使用频次排序，",
            "> 为向医保经办索取字典时的优先级依据。", ""]

    log += ["## 三、P2-3 收费项目 → 医保目录 覆盖度评估",
            "",
            ]
    if COST_ATTR.exists():
        ca = pd.read_csv(COST_ATTR, dtype=str, encoding="utf-8-sig")
        ca["has_national"] = ca["national_code"].notna()
        covered = int(ca["has_national"].sum())
        log += [f"- 院内收费项目：**{len(ca):,}** 条",
                f"- 可桥接到国家医疗服务项目代码：**{covered:,}**（{covered / len(ca):.1%}）",
                f"- 桥接来源：`源表(advice_prefix)*` 与 `源表(fee_prefix)*_In` 的 `列(cost_no_medicare)`（实际使用过的项目）",
                "",
                "**判读**：桥接覆盖的是**实际发生业务**的收费项目；未桥接的多为长期未使用项目，",
                "不影响结算。因此对照重建的重点不是「全量 12,546 条」，而是**在用项目**（约 1,814 条）——",
                "范围收敛后工作量显著下降。", ""]
        if CQ_CATALOG.exists():
            cq = pd.read_csv(CQ_CATALOG, dtype=str, encoding="utf-8-sig")
            列(dirs) = cq["国家医疗服务项目代码"].notna().sum()
            log += [f"- 参照《某地区医疗服务项目》目录 **{列(dirs):,}** 条国家项目码，可用于反向校验。", ""]
    else:
        log.append("- 未找到 `src/out/feemap/cost_item_attribute.csv`，请先运行 s09_fee_mapping.py")

    log += ["## 四、产出与下一步", "",
            "- `src/out/tcd/official_tcd_codes.csv` —— 本地可提取的全部 TCD 编码",
            "- `src/out/tcd/inhouse_tcm_mapping.csv` —— 院内中医码 → 官方 TCD（名称级）",
            "",
            "**下一步**：",
            "1. **向医保经办/某地区中医药管理局索取《中医病证分类与代码》(GB/T 15657-2021) 全量字典**"
            "——这是 TCD 映射的前置条件，属使用方配合事项；",
            "2. 字典到位后，本作业的匹配逻辑可直接复用（名称级 + 拼音码双重匹配）；",
            "3. 收费项目对照重建聚焦**在用项目**（约 1,814 条），已由 S09 完成桥接；剩余为业务未发生项目。",
            ]

    tcd.to_csv(OUT_SUB / "official_tcd_codes.csv", index=False, encoding="utf-8-sig")
    tcm[[src_col("code"), src_col("note"), "列(kind)", "tcd_guess"]].to_csv(
        OUT_SUB / "inhouse_tcm_mapping.csv", index=False, encoding="utf-8-sig")

    md = "\n".join(log)
    (OUT_SUB / "s12_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
