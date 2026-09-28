"""Unified DRG grouping engine.

Two grouping paths, evaluated in order:
  1. Chongqing TCM advantage groups (某地区医保局〔2025〕27号) - a local overlay on top of the
     national CHS-DRG rules, so it must be tried first;
  2. Standard CHS-DRG 2.0: main diagnosis -> MDC -> ADRG (surgical conditions first) ->
     severity from CC/MCC -> DRG code -> weight -> payment standard.

Dictionaries are read from src/config/dict/ (no Excel dependency at runtime) and are
produced by the extraction jobs s03 / s04 / s11.
Docs: docs/18-P2分组引擎与映射重建.md, docs/19-引擎接入在院模拟器.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DICT = PROJECT_DIR / "config" / "dict"
OUT_TCM = PROJECT_DIR / "out" / "tcm"

CATALOG_CSV = CONFIG_DICT / "drg_group_catalog.csv"
QY_CSV = CONFIG_DICT / "drg_qy_groups.csv"
TCM_RULES_JSON = (CONFIG_DICT / "tcm_advantage_rules.json"
                  if (CONFIG_DICT / "tcm_advantage_rules.json").exists()
                  else OUT_TCM / "tcm_advantage_rules.json")

# Settlement parameters, cross-verified in S01/S04 (N cases and the annex-2 fees).
FEE_RATE = 9575.25
COEF = 0.92

SEV_MCC, SEV_CC, SEV_NONE = "1", "3", "5"
SEV_UNREFINED = "9"          # national placeholder when the local list has no refinement
SEV_LABEL = {
    SEV_MCC: "伴严重并发症或合并症",
    SEV_CC: "伴一般并发症或合并症",
    SEV_NONE: "不伴并发症或合并症",
    SEV_UNREFINED: "未细分严重程度",
}
# Fallback chain per detected severity, used when the ADRG has no exact digit. Chongqing
# merges "severe" and "general" into digit 3 for medical groups (e.g. IU23), while the
# national table only ships the unrefined digit 9 for those same groups.
SEV_FALLBACK = {
    SEV_MCC: (SEV_MCC, SEV_CC, SEV_UNREFINED, SEV_NONE),
    SEV_CC: (SEV_CC, SEV_MCC, SEV_UNREFINED, SEV_NONE),
    SEV_NONE: (SEV_NONE, SEV_UNREFINED, SEV_CC, SEV_MCC),
}

# TCM operation classes (27号文): 0 bone-setting, 1 tuina, 2 needling, 3 moxibustion,
# 4 dressing/fumigation.
BONE_CLASS = "0"
TREAT_CLASSES = ("1", "2", "3", "4")

DICT_FILES = {
    "cond": "drg_cond.csv",
    "drg": "drg_drg.csv",
    "adrg": "drg_adrg.csv",
    "main_dx": "drg_main_dx.csv",
    "cc": "drg_cc.csv",
    "mcc": "drg_mcc.csv",
    "exclude": "drg_exclude.csv",
    "no_main_dx": "drg_no_main_dx.csv",
    "no_main_op": "drg_no_main_op.csv",
}

PATH_TCM, PATH_STD, PATH_NONE = "tcm", "standard", "none"


def norm(c: object) -> str:
    return str(c).strip().upper()


def load_dictionaries(dict_dir: Path = CONFIG_DICT) -> dict[str, pd.DataFrame] | None:
    """Read the official dictionaries; None when they have not been extracted yet."""
    out: dict[str, pd.DataFrame] = {}
    for key, fname in DICT_FILES.items():
        p = dict_dir / fname
        if not p.exists():
            return None
        out[key] = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
    return out


class StandardGrouper:
    """National CHS-DRG 2.0 grouping (without the TCM overlay)."""

    def __init__(self, dic: dict[str, pd.DataFrame], catalog: pd.DataFrame,
                 qy: set[str] | None = None):
        # QY pseudo-groups carry no weight but are the official outcome for an ungroupable case
        self.qy = qy or set()
        self.n_groups = len(catalog)
        self.weight_23: dict[str, float] = {}
        self.weight_1: dict[str, float] = {}
        self.drg_name: dict[str, str] = {}
        for r in catalog.itertuples():
            code = str(r.drg_code)
            self.drg_name[code] = str(r.drg_name)
            try:
                self.weight_23[code] = float(r.weight_23)
            except (TypeError, ValueError):
                pass
            try:
                self.weight_1[code] = float(r.weight_1)
            except (TypeError, ValueError):
                pass

        self.mdc_by_dx: dict[str, set[str]] = {}
        for r in dic["main_dx"].itertuples():
            self.mdc_by_dx.setdefault(norm(r.code), set()).add(r.mdc)

        self.op_cond: dict[str, set[str]] = {}
        self.dx_cond: dict[str, set[str]] = {}
        self.adrg_mdc: dict[str, str] = {}
        self.adrg_order: dict[str, int] = {}
        for i, r in enumerate(dic["cond"].itertuples()):
            tgt = self.op_cond if r.kind == "op" else self.dx_cond
            tgt.setdefault(r.adrg, set()).add(norm(r.code))
            self.adrg_mdc.setdefault(r.adrg, r.mdc)
            self.adrg_order.setdefault(r.adrg, i)

        self.drg_by_adrg: dict[str, dict[str, str]] = {}
        for r in dic["drg"].itertuples():
            self.drg_by_adrg.setdefault(r.adrg, {})[str(r.drg)[-1]] = r.drg
        # The national DRG table lists only the unrefined '9' variant for medical groups;
        # Chongqing refines them locally (IU2 -> IU23 / IU25), so merge both sources.
        # Non-numeric suffixes (F/R/Z) are TCM-specific and must not enter the severity chain.
        for r in catalog.itertuples():
            code = str(r.drg_code)
            if code[-1].isdigit():
                self.drg_by_adrg.setdefault(code[:3], {}).setdefault(code[-1], code)

        self.adrg_name = dict(zip(dic["adrg"]["adrg"], dic["adrg"]["adrg_name"]))
        self.mcc = {norm(x) for x in dic["mcc"]["code"] if x}
        self.cc = {norm(x) for x in dic["cc"]["code"] if x}
        self.exclude = {norm(x) for x in dic["exclude"]["code"] if x}

        # Each CC/MCC row names the exclusion table it belongs to (表 6-3-N), and that table
        # lists the PRINCIPAL diagnoses for which this complication must not be counted.
        # Union-ing every table into one global blacklist (the previous behaviour) removes
        # almost every comorbidity - measured: 86/86 CC codes in the 53 under-call cases were
        # dropped that way. See docs/32 §P1.
        _excl_by_table: dict[str, set[str]] = {}
        for r in dic["exclude"].itertuples():
            _excl_by_table.setdefault(str(r.excl).strip(), set()).add(norm(r.code))
        self.cc_excl: dict[str, set[str]] = {}
        for key in ("cc", "mcc"):
            df = dic[key]
            if "excl" not in df.columns:
                continue
            for r in df.itertuples():
                tbl = str(r.excl).strip()
                if tbl:
                    self.cc_excl[norm(r.code)] = _excl_by_table.get(tbl, set())
        self.no_main_dx = {norm(x) for x in dic["no_main_dx"]["code"] if x}
        self.no_main_op = {norm(x) for x in dic["no_main_op"]["code"] if x}

    def severity(self, other_dx: set[str], main_dx: str | None = None) -> str:
        """MCC / CC / none.

        The exclusion table is keyed per CC/MCC code: a complication is dropped only when
        the principal diagnosis appears in *that code's own* exclusion list. Applying the
        exclusion list globally used to cancel nearly every comorbidity.
        """
        md = norm(main_dx) if main_dx else ""
        cands = {norm(x) for x in other_dx}
        # Standard reading: the exclusion table of a CC/MCC lists the related diagnoses
        # (typically principal) whose presence cancels that complication - e.g. M81.900
        # 骨质疏松 is cancelled by 表 6-3-134 holding M80.x 骨质疏松伴病理性骨折.
        for d in cands:
            if d in self.mcc and md not in self.cc_excl.get(d, set()):
                return SEV_MCC
        for d in cands:
            if d in self.cc and md not in self.cc_excl.get(d, set()):
                return SEV_CC
        return SEV_NONE

    def mdc_of(self, main_dx: str | None) -> set[str]:
        return self.mdc_by_dx.get(norm(main_dx), set()) if main_dx else set()

    def resolve_drg(self, adrg: str, sev: str) -> tuple[str | None, str]:
        """Pick the DRG code for a severity, falling back when the digit is unknown."""
        variants = self.drg_by_adrg.get(adrg, {})
        for digit in SEV_FALLBACK.get(sev, (sev,)):
            if digit in variants:
                return variants[digit], digit
        return None, sev

    def group(self, main_dx: str | None, ops: set[str], other_dx: set[str]) -> dict:
        dx = norm(main_dx) if main_dx else ""
        mdcs = self.mdc_of(main_dx)
        ops_n = {norm(o) for o in ops}

        cands: list[tuple[int, str]] = []          # (priority, adrg)
        for adrg, codes in self.op_cond.items():
            if self.adrg_mdc.get(adrg) in mdcs and (codes & ops_n):
                cands.append((0, adrg))            # surgical groups take precedence
        if not cands:
            for adrg, codes in self.dx_cond.items():
                if self.adrg_mdc.get(adrg) in mdcs and dx in codes:
                    cands.append((1, adrg))

        if not cands:
            qy = next((f"{m}QY" for m in sorted(mdcs) if f"{m}QY" in self.qy), None)
            if not mdcs:
                reason = "无 MDC/ADRG 命中"
            elif qy:
                reason = f"命中 MDC 但无 ADRG 条件匹配 → 判为 {qy} 未入组"
            else:
                reason = "命中 MDC 但无 ADRG 条件匹配"
            return {"adrg": qy, "adrg_name": "", "severity": None, "drg": None,
                    "qy": qy, "reason": reason}
        cands.sort(key=lambda t: (t[0], self.adrg_order.get(t[1], 9999)))
        adrg = cands[0][1]
        sev = self.severity(other_dx, main_dx)
        drg, sev_used = self.resolve_drg(adrg, sev)
        if not drg:
            reason = f"{adrg} 无可用严重程度细分组"
        elif sev_used != sev:
            reason = f"{adrg} 无第 {sev} 位（{SEV_LABEL.get(sev, '')}），回退第 {sev_used} 位"
        else:
            reason = ""
        return {"adrg": adrg, "adrg_name": self.adrg_name.get(adrg, ""), "severity": sev_used,
                "sev_detected": sev, "drg": drg, "qy": None, "reason": reason}


class TcmGrouper:
    """Chongqing TCM advantage groups (local overlay, evaluated before the standard path)."""

    def __init__(self, rules: dict):
        self.rules = rules
        self._ordered = sorted(rules.items(), key=lambda kv: kv[1]["order"])
        self._dx = {c: {norm(d["code"]) for d in gg["dx"]} for c, gg in rules.items()}

    def group(self, main_dx: str | None, classes: set[str],
              days: float | None) -> dict:
        # days may arrive as NaN (the source column is empty for some cases). `NaN < threshold`
        # is False, so a NaN would silently PASS every group's day gate and admit the case to a
        # TCM group whose length-of-stay condition it never satisfied (code review Q4).
        if not main_dx or days is None or days != days:
            return {"drg": None, "reason": "缺少主诊断或住院天数"}
        dx = norm(main_dx)
        for c, gg in self._ordered:
            if days < (gg["days"] or 0):
                continue
            if dx not in self._dx[c]:
                continue
            if gg["op_mode"] == "combo":
                allowed = {k for k in gg["ops"] if k != "main"} & set(TREAT_CLASSES)
                if len(classes & allowed) >= gg.get("min_ops", 99):
                    return {"drg": c, "name": gg["name"], "reason": ""}
            elif BONE_CLASS in classes:
                return {"drg": c, "name": gg["name"], "reason": ""}
        return {"drg": None, "reason": "未命中中医优势病组入组条件"}


class DrgEngine:
    """Facade used by the in-hospital simulator and the back-test jobs."""

    def __init__(self, dict_dir: Path = CONFIG_DICT, rules_json: Path | None = None):
        dic = load_dictionaries(dict_dir)
        catalog_csv = dict_dir / CATALOG_CSV.name
        self.ready = dic is not None and catalog_csv.exists()
        if not self.ready:
            self.std = None
            self.tcm = None
            self.n_groups = 0
            return
        catalog = pd.read_csv(catalog_csv, dtype=str, encoding="utf-8-sig").fillna("")
        qy_csv = dict_dir / QY_CSV.name
        qy = set()
        if qy_csv.exists():
            qy = {str(c).strip() for c in
                  pd.read_csv(qy_csv, dtype=str, encoding="utf-8-sig")["qy_code"].dropna()}
        self.std = StandardGrouper(dic, catalog, qy)
        self.n_groups = self.std.n_groups
        rj = rules_json or (
            dict_dir / TCM_RULES_JSON.name if (dict_dir / TCM_RULES_JSON.name).exists()
            else TCM_RULES_JSON)
        self.tcm = TcmGrouper(json.loads(Path(rj).read_text(encoding="utf-8"))) \
            if Path(rj).exists() else None

    def weight_of(self, drg: str | None) -> float | None:
        if not drg or self.std is None:
            return None
        return self.std.weight_23.get(drg)

    def payment_standard(self, drg: str | None) -> float | None:
        w = self.weight_of(drg)
        return round(w * FEE_RATE * COEF, 2) if w else None

    def drg_name_of(self, drg: str | None) -> str:
        if not drg or self.std is None:
            return ""
        return self.std.drg_name.get(drg, "")

    def group(self, *, main_dx: str | None = None, ops: set[str] | None = None,
              other_dx: set[str] | None = None, tcm_classes: set[str] | None = None,
              days: float | None = None) -> dict:
        """Return the grouping result with the path that produced it."""
        if not self.ready:
            return {"path": PATH_NONE, "drg": None, "reason": "引擎字典未就绪（先运行 s11_adrg.py）"}

        if self.tcm is not None:
            t = self.tcm.group(main_dx, set(tcm_classes or []), days)
            if t.get("drg"):
                return {"path": PATH_TCM, "drg": t["drg"], "adrg": t["drg"][:3],
                        "adrg_name": t.get("name", ""), "severity": t["drg"][-1],
                        "reason": ""}

        res = self.std.group(main_dx, set(ops or []), set(other_dx or []))
        return {"path": PATH_STD if res.get("drg") else PATH_NONE,
                "drg": res.get("drg"), "adrg": res.get("adrg"), "qy": res.get("qy"),
                "adrg_name": res.get("adrg_name", ""), "severity": res.get("severity"),
                "sev_detected": res.get("sev_detected"), "reason": res.get("reason", "")}
