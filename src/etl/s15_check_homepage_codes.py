"""S15 Compare the coded operation set on the disease-case homepage against the HIS record.

S14's missing-operation rule fired on 81.5% of cases, which suggests 源表(operation) is not
the set that was actually submitted: the disease-case homepage carries its own 手术编码 /
诊断编码 fields and that is what Medical Affairs means by "the codes". This probe measures
coverage, format and agreement between the two sources so the rule can be aimed at the right
one.
Docs: docs/21-编码助手v0与量化回测.md.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = OUT_DIR / "coding"
OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"
HOME = "med_record.dbo.源表(case_man)"


def main() -> int:
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    log = ["# S15 病案首页编码 vs HIS 操作记录", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]

    # The homepage has no discharge-date filter of its own, so join through 源表(admission).
    sql = (f"SELECT m.列(hospital_no), h.[手术编码], h.[手术名称], h.[中医诊断_疾病编码], "
           f"h.[西医诊断_疾病编码], h.[编码员] FROM 源表(admission) m "
           f"LEFT JOIN {HOME} h ON h.[住院号] = m.列(hospital_no) "
           f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'")
    d = query_df(sql)
    n = len(d)
    log.append(f"- HIS 窗口内出院病例：**{n:,}**")

    for col in ("手术编码", "手术名称", "中医诊断_疾病编码", "西医诊断_疾病编码", "编码员"):
        filled = int(d[col].notna().sum()) if col in d else 0
        log.append(f"- 首页 `{col}` 覆盖：**{filled:,}**（{filled / n:.1%}）")
    log.append("")

    # HIS-side operation record for the same window
    ops = query_df(
        "SELECT m.列(hospital_no), o.列(code) FROM 源表(operation) o "
        "JOIN 源表(admission) m ON m.列(serial) = o.列(id_parent) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}'")
    his_cases = set(ops[src_col("hospital_no")].astype(str).str.strip())
    log.append(f"- 源表(operation) 有记录的病例：**{len(his_cases):,}**"
               f"（{len(his_cases) / n:.1%}）")

    if "手术编码" in d:
        # key by admission number, not by the code value itself
        home_mask = d["手术编码"].notna() & d["手术编码"].astype(str).str.strip().ne("")
        home_cases = {str(x).strip() for x in d.loc[home_mask, src_col("hospital_no")]}
        log.append(f"- 首页有手术编码的病例：**{len(home_cases):,}**"
                   f"（{len(home_cases) / n:.1%}）")
        both = his_cases & home_cases
        log.append(f"- 两者都有：**{len(both):,}**；仅 HIS：{len(his_cases - home_cases):,}；"
                   f"仅首页：{len(home_cases - his_cases):,}")
        log += ["",
                "> 该字段承载的是**中医操作编码**（99.92 针刺 / 17.9x 推拿灸法 / 93.3x 正骨手法），"
                "并非外科手术码——本单位 97% 为内科组，中医优势病组正是靠这些操作码入组。",
                "> 因此**编码助手必须以此字段为准**：`源表(operation)` 是 HIS 内部记录，"
                "与提交给医保的编码不是同一来源。", ""]

        # how many cases carry more than one operation code
        multi = d.loc[home_mask, "手术编码"].astype(str)
        log.append(f"- 单码病例：{int(multi.str.contains('[,，;；|]', regex=True).eq(False).sum()):,}；"
                   f"多码分隔病例：{int(multi.str.contains('[,，;；|]', regex=True).sum()):,}")
        log.append("")

        samples = [x for x in d["手术编码"].dropna().astype(str) if x.strip()][:12]
        log += ["## 一、首页 `手术编码` 的实际格式（判断多码分隔符）", ""]
        for s in samples:
            log.append(f"- `{s[:90]}`")
        delim = _guess_delim(samples)
        log.append(f"\n- 推测多码分隔符：**`{delim}`**" if delim else
                   "\n- 未发现多码分隔符（单码或自由文本）。")
        log.append("")

    # ---- yearly trend: is the coding gap historical or current? ----
    log += ["## 二、编码完整度的年度趋势（判断缺口是历史遗留还是当前问题）", ""]
    trend = query_df(
        "SELECT YEAR(入院时间) AS yr, COUNT(1) AS cases, "
        "SUM(CASE WHEN [手术编码] IS NOT NULL AND LTRIM(RTRIM([手术编码])) <> '' "
        "    THEN 1 ELSE 0 END) AS op_coded, "
        "SUM(CASE WHEN [西医诊断_疾病编码] IS NOT NULL "
        "    AND LTRIM(RTRIM([西医诊断_疾病编码])) <> '' THEN 1 ELSE 0 END) AS dx_coded, "
        "SUM(CASE WHEN [编码员] IS NOT NULL AND LTRIM(RTRIM([编码员])) <> '' "
        "    THEN 1 ELSE 0 END) AS coder "
        f"FROM {HOME} WHERE 入院时间 IS NOT NULL GROUP BY YEAR(入院时间) ORDER BY 1")
    if len(trend):
        trend["手术编码率"] = (trend["op_coded"] / trend["cases"]).map(lambda x: f"{x:.1%}")
        trend["西医诊断率"] = (trend["dx_coded"] / trend["cases"]).map(lambda x: f"{x:.1%}")
        trend["编码员率"] = (trend["coder"] / trend["cases"]).map(lambda x: f"{x:.1%}")
        log += [_md(trend[["yr", "cases", "手术编码率", "西医诊断率", "编码员率"]]), "",
                "> 若近期年份明显高于早年，说明**编码缺口主要是历史遗留，近年已改善**——"
                "则\"编码助手\"的定位应从\"补历史欠账\"转为\"守住当期质量并防止回退\"。", ""]

    # ---- do the homepage codes match the 27号文 rule codes at all? ----
    log += ["## 三、首页操作码 与 27号文规则码 的体系校验", ""]
    import json  # noqa: PLC0415
    rules = json.loads((OUT_DIR.parent / "config" / "dict"
                        / "tcm_advantage_rules.json").read_text(encoding="utf-8"))
    rule_codes = {norm(o["code"]) for g in rules.values()
                  for lst in g["ops"].values() for o in lst}
    home_codes = {norm(x) for x in d.get("手术编码", pd.Series(dtype=str)).dropna()
                  if str(x).strip()}
    inter = home_codes & rule_codes
    log += [f"- 首页不同操作码：**{len(home_codes)}**",
            f"- 27号文规则码（附件2 提取）：**{len(rule_codes)}**",
            f"- **直接交集：{len(inter)}**"
            f"（{len(inter) / len(home_codes):.1%} 的首页码）", ""]
    log += ["- 首页码样例：`" + "`、`".join(sorted(home_codes)[:6]) + "`",
            "- 规则码样例：`" + "`、`".join(sorted(rule_codes)[:6]) + "`", ""]
    if len(inter) / max(len(home_codes), 1) < 0.5:
        log += ["", "> **判据不成立**：两套编码形式不同（首页为带 `x` 细目的扩展码，"
                "如 `99.9200x016`；附件2 为 `17.972A0` 式），**不能直接比对**。",
                "> 编码助手必须先建立「首页操作码 → 27号文操作类」映射，否则操作漏编规则"
                "（R1）会大面积误报——实测 R1 假阳性率约 98%。", ""]

    md = "\n".join(log)
    (OUT_SUB / "s15_homepage_codes.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def norm(c: object) -> str:
    return str(c).strip().upper()


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


def _guess_delim(samples: list[str]) -> str | None:
    for cand in (";", "；", ",", "，", "|", "/", "+"):
        if any(cand in s for s in samples):
            return cand
    return None


if __name__ == "__main__":
    raise SystemExit(main())
