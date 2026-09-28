"""Output guard: makes the whitelist a control instead of a document.

The 审议 mechanism in docs/26 is only worth anything if the system actually refuses to emit an
output that has not been confirmed. Until this module existed, nothing in src/etl/ read
whitelist_rules.csv at all - the simulator had been printing its four lines, including the
group/payment statement and the fee forecast, whether or not any rule covered them. The
whitelist was a review artefact, not a control.

Every user-facing line is therefore tagged with the whitelist rule it comes from, and a line is
only emitted when:
  1. its rule exists and its status is 已确认 (not 待审议, not 二期); and
  2. its text does not match a blacklist pattern (B01-B09).

Anything else is suppressed, recorded in suppressed_outputs, and reported - so a rule that is
pulled back for re-review stops appearing immediately, without a code change.

Rules live in src/config/{whitelist,blacklist}_rules.csv so 医务科/信息科 can change what the
system is allowed to say by editing a spreadsheet, with the audit trail (confirmed_by /
confirmed_date) in the same file.
Docs: docs/26-系统输出内容审议材料.md.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR  # noqa: E402

WHITELIST_CSV = CONFIG_DIR / "whitelist_rules.csv"
BLACKLIST_CSV = CONFIG_DIR / "blacklist_rules.csv"

STATUS_CONFIRMED = "已确认"

# Blacklist patterns, hand-maintained from blacklist_rules.csv B01-B09. Kept explicit rather
# than parsed out of the prose `forbidden_output` column: parsing produced false positives on
# ordinary words like 建议, and a guard that cries wolf gets switched off.
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    # B01 诱导增加。真实文案常常不带"建议"两字——"增加中药饮片处方可提升中药饮片费"
    # 就是一条能绕过"建议.+增加"的诱导，故按动词本身匹配，不再依赖引导词。
    (r"(增加|加做|多做|补做|追加).{0,14}(中医|治疗|检查|用药|项目|操作|毫针|推拿|灸|拔罐|处方)", "B01"),
    (r"(提升|提高).{0,12}(中医治疗费|中成药费|中药饮片费|费用占比)", "B01"),
    (r"建议.{0,10}(增加|加做|多做|补做)", "B01"),
    # B02 延长住院。"继续住院治疗至达标"同样绕过只匹配"再住/延长"的初版
    (r"(再住|多住|延长住院|继续住院|继续住|住满\s*\d|住院治疗至)", "B02"),
    # B03 改诊断。两种语序都要覆盖："把主诊断改成XX"与"改成XX诊断"。
    # 注意"主要诊断"不含子串"主诊断"，故复核依据类合法措辞不会被误伤。
    (r"主诊断.{0,4}(改成|改为|换成|调整)", "B03"),
    (r"(改成|改为|换成|调整为).{0,10}诊断", "B03"),
    (r"(调整|修改|变更).{0,6}主诊断", "B03"),
    # B04 虚构中医操作
    (r"(再补|加做|多做).{0,10}(毫针|推拿|灸|拔罐|操作)", "B04"),
    # B09 反向诱导：为凑指标而减少/替代西医诊疗
    (r"(减少|缩减|控制|降低).{0,10}(诊疗|检查|用药|治疗|服务|费用|占比)", "B09"),
    (r"替代.{0,6}西医", "B09"),
    (r"提前出院|推诿", "B09"),
]

# A gap statement on its own is a fact and is allowed - but some rules mandate a specific
# disclaimer, without which the same words read as advice. W04 is exactly that case: saying
# "住院天数不足，还差 7 天" is fine, saying it without "不做人为干预" tells the physician to
# extend the stay. (rule -> (topic keyword, mandatory wording))
REQUIRED_PHRASE: dict[str, tuple[str, str]] = {
    "W04": ("住院天数", "不做人为干预"),
}

_suppressed: list[dict] = []


def _load(csv: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(csv, dtype=str, encoding="utf-8-sig").fillna("")
    except Exception:  # noqa: BLE001  - a missing rules file must not crash a job outright
        return pd.DataFrame()


class Guard:
    def __init__(self) -> None:
        self.wl = _load(WHITELIST_CSV)
        self.bl = _load(BLACKLIST_CSV)
        self.available = "rule_id" in self.wl.columns

    def status_of(self, rule_id: str) -> str:
        if not self.available:
            return ""
        row = self.wl[self.wl["rule_id"] == rule_id]
        return str(row.iloc[0]["status"]).strip() if len(row) else ""

    def confirmed(self, rule_id: str) -> bool:
        return self.status_of(rule_id) == STATUS_CONFIRMED

    @staticmethod
    def blacklist_hit(text: str) -> str:
        for pat, bid in FORBIDDEN_PATTERNS:
            if re.search(pat, text or ""):
                return bid
        return ""

    def screen(self, rule_id: str, text: str) -> tuple[bool, str]:
        """(allowed, reason) - reason is empty when allowed."""
        if not text:
            return True, ""
        st = self.status_of(rule_id)
        if not st:
            return False, f"无白名单规则 {rule_id}"
        if st != STATUS_CONFIRMED:
            return False, f"白名单 {rule_id} 状态为 {st}（需 已确认 方可输出）"
        bid = self.blacklist_hit(text)
        if bid:
            return False, f"命中黑名单 {bid}"
        req = REQUIRED_PHRASE.get(rule_id)
        if req and req[0] in text and req[1] not in text:
            return False, f"白名单 {rule_id} 要求措辞「{req[1]}」，缺失不得输出"
        return True, ""

    def guard(self, rule_id: str, text: str, fallback: str = "", *,
              where: str = "") -> str:
        """Return the text only if it may be shown, else `fallback` - and remember why."""
        ok, reason = self.screen(rule_id, text)
        if ok:
            return text
        _suppressed.append({"rule_id": rule_id, "where": where,
                            "reason": reason, "text": str(text)[:160]})
        return fallback


def suppressed() -> pd.DataFrame:
    return pd.DataFrame(_suppressed)


def reset() -> None:
    _suppressed.clear()
