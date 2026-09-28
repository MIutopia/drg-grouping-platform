"""S16 Build the missing link: homepage operation codes -> 27号文 TCM operation classes.

S14's missing-operation rule (R1) fires on 450 of N cases but only 7 are confirmed, because
the code sets do not speak the same language:
  - the disease-case homepage carries 40 distinct codes such as 17.9200X024;
  - annex 2 of 某地区医保局〔2025〕27号 carries 83 codes such as 17.91240.
Their direct intersection is 2 codes (5.0%), so no rule can compare them as-is.

This job produces src/config/dict/homepage_op_class.csv, mapping every homepage code to one of
the five TCM operation classes (0 bone-setting ... 4 dressing/fumigation), and validates the
result by re-running the S14 back-test with the corrected evidence.
Docs: docs/22-首页操作码映射与R1修正.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR  # noqa: E402

OUT_SUB = OUT_DIR / "coding"
DICT_OUT = CONFIG_DIR / "dict"
RULES_JSON = DICT_OUT / "tcm_advantage_rules.json"
HOME = "med_record.dbo.源表(case_man)"

OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"

# Class is decided by the operation NAME, never by the code segment. Measured: the homepage
# packs unrelated families into one segment (93.35 moxibustion, 93.39 dressing, 93.54 fixation,
# 17.92 tuina, 17.97 bone-setting), so a prefix rule misclassifies most rows.
# Patterns are ordered - the first match wins, so the more specific families come first.
# Note "固定" alone is deliberately absent: it would swallow 内固定装置去除术 (real surgery).
NAME_RULES = [
    (r"手法整复|脱位|骨折.*复位|正骨|夹板|石膏托|牵引|悬吊", "0", "手法整复/正骨/固定牵引"),
    (r"推拿|按摩|松解术|导引|点穴|捏脊", "1", "推拿/松解"),
    (r"针刀|刃针|松解针|针刺|针法|电针|毫针|三棱针|火针|带刃针", "2", "针刺/针刀"),
    (r"灸", "3", "灸法"),
    (r"贴敷|敷贴|熏洗|熏药|拔罐|烫熨|砭石|穴位注射|透入|外治", "4", "敷贴/熏洗/拔罐等外治"),
]


def norm(c: object) -> str:
    return str(c).strip().upper()


def load_rules() -> dict:
    return json.loads(RULES_JSON.read_text(encoding="utf-8"))


def rule_op_table() -> pd.DataFrame:
    """Rule operation codes with their class and the groups that require them."""
    rows = []
    for grp, gg in load_rules().items():
        for key, lst in gg["ops"].items():
            cls = key if key in ("0", "1", "2", "3", "4") else "0"
            for o in lst:
                rows.append({"rule_code": norm(o["code"]), "rule_name": str(o["name"]).strip(),
                             "op_class": cls, "required_by": grp})
    return pd.DataFrame(rows).drop_duplicates(["rule_code", "op_class"])


def homepage_op_table() -> pd.DataFrame:
    d = query_df(
        f"SELECT h.[手术编码] AS code, h.[手术名称] AS name, COUNT(1) AS n FROM 源表(admission) m "
        f"JOIN {HOME} h ON h.[住院号] = m.列(hospital_no) "
        f"WHERE m.列(time_out) >= '{OUT_FROM}' AND m.列(time_out) < '{OUT_TO}' "
        "AND h.[手术编码] IS NOT NULL AND LTRIM(RTRIM(h.[手术编码])) <> '' "
        "GROUP BY h.[手术编码], h.[手术名称] ORDER BY COUNT(1) DESC")
    d["code"] = d["code"].map(norm)
    d["name"] = d["name"].fillna("").astype(str).str.strip()
    return d


def classify_name(name: str) -> tuple[str | None, str]:
    for pat, cls, why in NAME_RULES:
        if re.search(pat, name):
            return cls, f"名称命中「{why}」"
    return None, ""


# ------------------------------------------------------------------
def probe() -> int:
    hp = homepage_op_table()
    rule = rule_op_table()
    log = ["# S16 首页操作码 ↔ 27号文规则码 对照探针", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
           f"- 首页不同操作码：**{hp['code'].nunique()}**（{OUT_FROM} ~ {OUT_TO}）",
           f"- 27号文规则码：**{rule['rule_code'].nunique()}**", "",
           "## 一、首页操作码全量（按例数降序）", "",
           "| 手术编码 | 手术名称 | 例数 | 编码段推断类 |", "|---|---|---:|---|"]
    for r in hp.itertuples():
        cls, _ = classify_name(r.name)
        log.append(f"| `{r.code}` | {r.name[:28]} | {int(r.n)} | {cls or '—'} |")

    log += ["", "## 二、27号文规则码全量", "",
            "| 规则码 | 名称 | 类 | 被哪些组要求 |", "|---|---|---:|---|"]
    for r in rule.sort_values(["op_class", "rule_code"]).itertuples():
        log.append(f"| `{r.rule_code}` | {r.rule_name[:28]} | {r.op_class} | {r.required_by} |")

    inter = set(hp["code"]) & set(rule["rule_code"])
    log += ["", "## 三、交集", "",
            f"- 直接交集：**{len(inter)}** 个 —— `{'`、`'.join(sorted(inter)) or '无'}`", ""]

    OUT_SUB.mkdir(parents=True, exist_ok=True)
    md = "\n".join(log)
    (OUT_SUB / "s16_probe.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump both code sets")
    a = ap.parse_args()
    if a.probe:
        return probe()
    return build()


def build() -> int:
    hp = homepage_op_table()
    rule = rule_op_table()
    log = ["# S16 首页操作码映射表构建", "",
           f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", ""]

    code_to = {r.rule_code: (r.op_class, r.rule_name, r.required_by)
               for r in rule.itertuples()}
    name_to = {}
    for r in rule.itertuples():
        name_to.setdefault(r.rule_name, (r.op_class, r.rule_code))

    mapped = []
    for r in hp.itertuples():
        cls, how, why = "", "", ""
        if r.code in code_to:
            c, nm, grp = code_to[r.code]
            cls, how, why = c, "①精确码", f"附件2 `{r.code}` {nm}（{grp}）"
        elif r.name and r.name in name_to:
            c, rc = name_to[r.name]
            cls, how, why = c, "②名称精确", f"与附件2 `{rc}`「{r.name}」同名"
        else:
            c, w = classify_name(r.name)
            if c:
                cls, how, why = c, "③名称关键词", w
        kind = "中医操作" if cls else ("非中医操作" if re.match(r"^(78|79|80|81|84)\.", r.code)
                                     else "待核")
        mapped.append({"手术编码": r.code, "手术名称": r.name, "例数": int(r.n),
                       "op_class": cls, "性质": kind, "匹配方式": how or "未匹配",
                       "依据": why})
    m = pd.DataFrame(mapped)
    m.to_csv(DICT_OUT / "homepage_op_class.csv", index=False, encoding="utf-8-sig")

    tcm = m[m["op_class"].ne("")]
    undecided = m[(m["op_class"].eq("")) & (m["性质"].eq("待核"))]
    log += [f"- 首页码 **{len(m)}** 个：中医操作 **{len(tcm)}**、非中医操作 "
            f"**{int(m['性质'].eq('非中医操作').sum())}**、待核 **{len(undecided)}**",
            f"- 按例数计：中医操作覆盖 **{int(tcm['例数'].sum())} / {int(m['例数'].sum())}**"
            f"（{int(tcm['例数'].sum()) / int(m['例数'].sum()):.1%}）", "",
            "## 一、映射结果", "", _md(m.sort_values("例数", ascending=False)), ""]
    if len(undecided):
        log += ["## 二、待核的码（需编码员/中医科确认）", "",
                _md(undecided[["手术编码", "手术名称", "例数"]]), ""]
    else:
        log += ["## 二、待核的码", "", "- 无 —— 全部首页码均已归类。", ""]
    md = "\n".join(log)
    (OUT_SUB / "s16_mapping.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def _md(df: pd.DataFrame) -> str:
    try:
        return df.to_markdown(index=False)
    except Exception:  # noqa: BLE001
        return "```\n" + df.to_string(index=False) + "\n```"


if __name__ == "__main__":
    raise SystemExit(main())
