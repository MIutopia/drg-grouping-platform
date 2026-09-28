"""L5 consistency audit: medical-record narrative vs. coded other-diagnoses.

Rule-only (no LLM). Two directions:
  - missed-coding clue : the narrative mentions a comorbidity that is absent from the
                         case's coded 其他诊断 -> emitted, gated by whitelist W07.
  - unsupported-code   : a coded 其他诊断 is never mentioned in the narrative -> counted
                         for internal QA only. It is deliberately NOT emitted: no
                         whitelist rule covers it, and telling anyone a diagnosis "lacks
                         support" reads as advice to remove it (blacklist B09).

The comorbidity dictionary is derived from the hospital's own coding history
(源表(diagnosis) other-diagnosis names) so no ICD code is ever invented.

Read-only. Docs: docs/31-AI赋能-财务问数与政策问答场景设计.md §1.8
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))

from dbio import query_df  # noqa: E402
from output_guard import Guard, suppressed  # noqa: E402
from settings import CONFIG_DIR  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

OUT_SUB = Path(__file__).resolve().parents[1] / "out" / "coding"
OUT_SUB.mkdir(parents=True, exist_ok=True)
DICT_CSV = CONFIG_DIR / "dict" / "other_dx_keywords.csv"

RULE_MISSED = "W07"          # whitelist: missed CC/MCC clue, "check only"
FIRST_COURSE = "首次病程记录"

# Noise in 列(note): trailing "？", punctuation, and laterality/quantity prefixes.
_PUNCT = re.compile(r"[？?。，,、；;：:（）()\[\]【】\s]+")
_PREFIX = re.compile(r"^(双侧|双|单|左|右|两侧|两)")

# "否认高血压、糖尿病、冠心病等慢性病史" is the single biggest false-positive source:
# the comorbidity is named precisely because the patient does NOT have it.
NEG_MARKERS = ("否认", "排除", "除外", "待排", "未见", "不伴", "否定",
               "无相关", "无上述", "无明显")
NEG_WINDOW = 40

# Core-keyword matching drops the laterality prefix, so 左侧膝关节骨性关节病 would also
# match "双侧膝关节骨性关节病". Reject when the text says a contradicting side.
_CONFLICT = {
    "左": ("双", "两侧", "两", "右"),
    "右": ("双", "两侧", "两", "左"),
    "双": ("左", "右"),
    "两": ("左", "右"),
}


def _laterality_conflict(name: str, ctx: str) -> bool:
    for side, bad in _CONFLICT.items():
        if name.startswith(side):
            return any(b in ctx for b in bad)
    return False


def _norm(s: str) -> str:
    return _PUNCT.sub("", str(s or ""))


def _core(name: str) -> str:
    """Strip laterality/quantity prefixes so 双膝关节退行性改变 also matches 膝关节退行性改变."""
    n = _norm(name)
    stripped = _PREFIX.sub("", n)
    return stripped if len(stripped) >= 3 else n


def _in(n: int) -> str:
    return ",".join(["%s"] * n)


# ------------------------------------------------------------------
# Dictionary
# ------------------------------------------------------------------
def build_dict(since: str, top: int) -> pd.DataFrame:
    """Derive the comorbidity dictionary from the hospital's own other-diagnosis history."""
    sql = f"""
    SELECT TOP {int(top)} d.列(icd) AS code, d.列(note) AS name, COUNT(1) AS freq
    FROM 源表(diagnosis) d
    WHERE d.列(order) > 1
      AND d.列(kind_diagnose) = 2
      AND d.列(kind_input) IN (3, 4)
      AND d.列(time_input) >= %s
      AND d.列(note) IS NOT NULL
      AND LTRIM(RTRIM(d.列(note))) <> ''
    GROUP BY d.列(icd), d.列(note)
    ORDER BY COUNT(1) DESC
    """
    df = query_df(sql, params=(since,))
    if df.empty:
        return df
    df["freq"] = pd.to_numeric(df["freq"], errors="coerce").fillna(0).astype(int)
    df["name"] = df["name"].map(_norm)
    df["keyword"] = df["name"]
    df["core"] = df["name"].map(_core)
    # drop unusable entries: empty, or purely numeric, or too short to match safely
    df = df[(df["name"].str.len() >= 3) & (df["core"].str.len() >= 3)]
    # one name may carry several codes (e.g. 高血压病 I10.x00x002 / I10.x00x021);
    # keeping them all would report the same finding once per code.
    df = (df.sort_values("freq", ascending=False)
            .drop_duplicates(subset=["name"], keep="first"))
    return df.reset_index(drop=True)


# ------------------------------------------------------------------
# Case inputs
# ------------------------------------------------------------------
def load_cases(since: str, limit: int) -> pd.DataFrame:
    sql = (
        f"SELECT TOP {int(limit)} 列(hospital_no), 列(doctor_residency), 列(time_out) "
        f"FROM 源表(admission) WHERE 列(time_out) >= %s AND 列(hospital_no) IS NOT NULL "
        f"ORDER BY 列(hospital_no)"
    )
    return query_df(sql, params=(since,))


def load_other_dx(nos: list[str]) -> dict[str, set[tuple[str, str]]]:
    """Other diagnoses per case, final version preferred, one row per 列(order)."""
    ph = _in(len(nos))
    sql = f"""
    WITH pick AS (
        SELECT 列(hospital_no), MAX(列(kind_input)) AS ki
        FROM 源表(diagnosis) WHERE 列(hospital_no) IN ({ph}) AND 列(kind_input) IN (3, 4)
        GROUP BY 列(hospital_no)
    )
    SELECT d.列(hospital_no), d.列(icd) AS code, d.列(note) AS name
    FROM 源表(diagnosis) d
    JOIN pick p ON p.列(hospital_no) = d.列(hospital_no) AND p.ki = d.列(kind_input)
    WHERE d.列(order) > 1 AND d.列(kind_diagnose) = 2
    """
    df = query_df(sql, params=nos)
    out: dict[str, set[tuple[str, str]]] = {n: set() for n in nos}
    for r in df.itertuples(index=False):
        out.setdefault(str(getattr(r, src_col("hospital_no"))), set()).add(
            (str(r.code or ""), _norm(r.name)))
    return out


def load_main_dx(nos: list[str]) -> dict[str, set[tuple[str, str]]]:
    """Primary diagnosis per case - a comorbidity equal to it is not a missed other-diagnosis."""
    ph = _in(len(nos))
    sql = f"""
    WITH pick AS (
        SELECT 列(hospital_no), MAX(列(kind_input)) AS ki
        FROM 源表(diagnosis) WHERE 列(hospital_no) IN ({ph}) AND 列(kind_input) IN (3, 4)
        GROUP BY 列(hospital_no)
    )
    SELECT d.列(hospital_no), d.列(icd) AS code, d.列(note) AS name
    FROM 源表(diagnosis) d
    JOIN pick p ON p.列(hospital_no) = d.列(hospital_no) AND p.ki = d.列(kind_input)
    WHERE d.列(order) = 1 AND d.列(kind_diagnose) = 2
    """
    df = query_df(sql, params=nos)
    out: dict[str, set[tuple[str, str]]] = {n: set() for n in nos}
    for r in df.itertuples(index=False):
        out.setdefault(str(getattr(r, src_col("hospital_no"))), set()).add(
            (str(r.code or ""), _norm(r.name)))
    return out


def load_narrative(nos: list[str]) -> dict[str, str]:
    """Concatenate 入院记录(既往史+现病史) and the longest 首次病程记录 per case."""
    ph = _in(len(nos))
    txt: dict[str, str] = {n: "" for n in nos}
    adm = query_df(
        "SELECT [住院号], ISNULL([既往史], '') + ' ' + ISNULL([现病史], '') AS t "
        f"FROM med_record.dbo.源表(med_record) WHERE [住院号] IN ({ph})",
        params=nos)
    for r in adm.itertuples(index=False):
        k = str(r.住院号)
        txt[k] = txt.get(k, "") + " " + str(r.t or "")

    course = query_df(
        "SELECT [住院号], MAX(LEN(ISNULL([病案内容], ''))) AS ln "
        "FROM med_record.dbo.hospital_course_record "
        f"WHERE [病案名称] = N'{FIRST_COURSE}' AND [住院号] IN ({ph}) GROUP BY [住院号]",
        params=nos)
    longest: dict[str, int] = {str(r.住院号): int(r.ln or 0) for r in course.itertuples(index=False)}
    if longest:
        ph2 = _in(len(longest))
        keys = list(longest.keys())
        c2 = query_df(
            "SELECT [住院号], [病案内容] AS t FROM med_record.dbo.hospital_course_record "
            f"WHERE [病案名称] = N'{FIRST_COURSE}' AND [住院号] IN ({ph2})",
            params=keys)
        for r in c2.itertuples(index=False):
            k = str(r.住院号)
            body = str(r.t or "")
            if len(body) >= longest.get(k, 0):
                txt[k] = txt.get(k, "") + " " + body
                longest[k] = -1  # keep only the longest one
    return {k: _norm(v) for k, v in txt.items()}


# ------------------------------------------------------------------
# Audit
# ------------------------------------------------------------------
def audit(cases: pd.DataFrame, dic: pd.DataFrame,
          dx: dict[str, set[tuple[str, str]]],
          main_dx: dict[str, set[tuple[str, str]]],
          txt: dict[str, str],
          guard: Guard) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    missed_rows, unsupported_rows = [], []
    neg_skipped = 0

    for c in cases.itertuples(index=False):
        no = str(getattr(c, src_col("hospital_no")))
        body = txt.get(no, "")
        own_codes = {x[0] for x in dx.get(no, set())}
        own_names = {x[1] for x in dx.get(no, set()) if x[1]}
        main_codes = {x[0] for x in main_dx.get(no, set())}
        main_names = {x[1] for x in main_dx.get(no, set()) if x[1]}
        if not body:
            continue

        for d in dic.itertuples(index=False):
            if d.code in own_codes or d.name in own_names:
                continue
            # already the primary diagnosis: correctly absent from 其他诊断.
            # containment, not equality - 颈椎间盘突出症 vs 颈椎间盘突出 等微小差异会漏网。
            if d.code in main_codes or d.name in main_names:
                continue
            if any(d.name and mn and (d.name in mn or mn in d.name) for mn in main_names):
                continue
            hit = ""
            if d.keyword and d.keyword in body:
                hit, conf = d.keyword, "高（名称精确命中）"
            elif d.core and d.core in body:
                hit, conf = d.core, "中（去方位词核心词命中）"
            else:
                continue
            pos = body.find(hit)
            if conf.startswith("中") and _laterality_conflict(d.name, body[max(0, pos - 8): pos]):
                continue
            pre = body[max(0, pos - NEG_WINDOW): pos]
            if any(m in pre for m in NEG_MARKERS):
                neg_skipped += 1
                continue
            snippet = body[max(0, pos - 30): pos + len(hit) + 30]
            text = (f"本例病历记录提及『{d.name}』（原文：…{snippet}…），其他诊断中未见对应编码"
                    f"（候选 {d.code}），请病案编码员核对是否应纳入填报"
                    f"（仅提示核对，不建议补写不存在的诊断）")
            shown = guard.guard(RULE_MISSED, text, fallback="", where=f"s32:{no}")
            if not shown:
                continue          # fail-closed: unconfirmed text is never emitted
            missed_rows.append({"住院号": no, "经管医师": getattr(c, src_col("doctor_residency")),
                                "出院日期": getattr(c, src_col("time_out")), "类型": "疑似漏编",
                                "诊断名": d.name, "候选编码": d.code,
                                "置信度": conf, "原文片段": snippet, "输出文本": shown})

        # Reverse direction: internal QA metric only, never emitted (see module docstring).
        for code, name in dx.get(no, set()):
            if name and len(name) >= 3 and name not in body and _core(name) not in body:
                unsupported_rows.append({"住院号": no, "类型": "依据不足(内部统计)",
                                         "诊断名": name, "编码": code})

    m = pd.DataFrame(missed_rows)
    if len(m):
        # one clue per (case, candidate code): variants like 腰椎间盘突出症 and
        # 腰椎间盘突出症L2/3-L5/S1 share M51.202 and would otherwise double up.
        m = m.drop_duplicates(subset=["住院号", "候选编码"], keep="first")
    return m, pd.DataFrame(unsupported_rows), neg_skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dict", action="store_true",
                    help="derive the comorbidity dictionary from HIS coding history")
    ap.add_argument("--audit", action="store_true", help="run the consistency audit")
    ap.add_argument("--since", default="2026-03-01")
    ap.add_argument("--top", type=int, default=60, help="dictionary size")
    ap.add_argument("--limit", type=int, default=300, help="max cases to audit")
    a = ap.parse_args()

    if not (a.build_dict or a.audit):
        ap.print_help()
        return 1

    if a.build_dict:
        d = build_dict(a.since, a.top)
        if d.empty:
            print("- 未生成词典（无数据）")
            return 1
        DICT_CSV.parent.mkdir(parents=True, exist_ok=True)
        d[["code", "name", "keyword", "core", "freq"]].to_csv(
            DICT_CSV, index=False, encoding="utf-8-sig")
        print(f"## 合并症词典（来源：本单位其他诊断历史，{a.since} 起）")
        print(f"- 条目 **{len(d)}**，已写入 {DICT_CSV}")
        print(d.head(15)[["code", "name", "freq"]].to_string(index=False))
        return 0

    if not DICT_CSV.exists():
        print(f"- 词典不存在，请先运行 --build-dict（目标 {DICT_CSV}）")
        return 1

    dic = pd.read_csv(DICT_CSV, dtype=str, encoding="utf-8-sig").fillna("")
    cases = load_cases(a.since, a.limit)
    if cases.empty:
        print("- 窗口内无出院病例")
        return 1
    nos = cases[src_col("hospital_no")].astype(str).tolist()
    dx = load_other_dx(nos)
    main_dx = load_main_dx(nos)
    txt = load_narrative(nos)
    print(f"- 病例 {len(cases)}，有病历文本 {sum(1 for v in txt.values() if v.strip())}，"
          f"词典 {len(dic)} 条")

    guard = Guard()
    missed, unsup, neg_skipped = audit(cases, dic, dx, main_dx, txt, guard)
    sup = suppressed()

    print(f"\n## 疑似漏编线索：**{len(missed)}** 条"
          f"（涉及病例 {missed['住院号'].nunique() if len(missed) else 0}）")
    print(f"- 其中剔除否定表述（否认/排除/未见…）：**{neg_skipped}** 次命中")
    if len(missed):
        print("\n### 按诊断名汇总")
        print(missed.groupby(["诊断名", "候选编码"]).size()
              .sort_values(ascending=False).head(15).to_string())
        print("\n### 明细（前 15 条）")
        print(missed.head(15)[["住院号", "诊断名", "候选编码", "置信度"]].to_string(index=False))

    print(f"\n## 依据不足（内部统计，不作为系统输出）：**{len(unsup)}** 条")
    print(f"## 被闸门抑制：**{len(sup)}** 条")
    if len(sup):
        print(sup[["rule_id", "reason"]].value_counts().to_string())

    out = OUT_SUB / "s32_dx_audit.csv"
    (missed if len(missed) else pd.DataFrame()).to_csv(out, index=False, encoding="utf-8-sig")
    OUT_SUB.mkdir(parents=True, exist_ok=True)
    (OUT_SUB / "s32_dx_audit.md").write_text(
        "\n".join([
            "# S32 L5 一致性稽核（病历文本 ↔ 其他诊断）", "",
            f"- 窗口：出院 >= {a.since}；病例 {len(cases)}；词典 {len(dic)} 条",
            f"- 疑似漏编线索：**{len(missed)}** 条（白名单 {RULE_MISSED} 放行）",
            f"- 依据不足：**{len(unsup)}** 条（内部统计，未申请白名单，不输出）",
            f"- 闸门抑制：**{len(sup)}** 条", "",
            "> 用药/文本仅为线索，不作为编码依据；任何增删须由编码员依据病历原文确认。",
        ]), encoding="utf-8")
    print(f"\n> 已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
