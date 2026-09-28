"""S27 Import the HR phonebook (钉钉通讯录 export) into drg.sys_doctor.

The responsible physician is the 住院医师, identified by mobile number instead of HIS job
number (docs/29). HIS getattr(SYS_USER, src_col("phone_mobi")) is empty, so the numbers come from the HR
phonebook, not from HIS. The join key back to HIS is the 员工工号 column, which was verified
to equal the "工号" prefix of 源表(admission).列(doctor_residency).

Scope: 住院部 clinical staff (department contains "住院部", title excludes nurses). Two active
physicians (周鹏 51009, 蒋明娥 51021) have an empty 员工工号 in this export, so their codes are
recovered from drg.result_insim (correct charset) by name before insert.

sys_doctor is a roster: each run replaces the table (truncate=True), so re-importing a newer
phonebook is a full sync, not a merge.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402

DEFAULT_FILE = Path.home() / "Desktop" / "示例中医医院有限公司-通讯录.xlsx"
SCOPE_KW = "住院部"          # department must contain this
NURSE_KW = "护士"            # and title must NOT contain this


def read_phonebook(path: str) -> pd.DataFrame:
    """Read the DingTalk export, skipping its two preamble rows to the real header."""
    raw = pd.read_excel(path, sheet_name=0, header=None)
    hdr = raw[raw.iloc[:, 0].astype(str).str.strip() == "员工UserId"].index[0]
    d = raw.iloc[hdr + 1:].copy()
    d.columns = raw.iloc[hdr].astype(str).str.strip()
    return d[d["姓名"].notna()]


def clean_phone(s: object) -> str | None:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    digits = re.sub(r"\D", "", str(s))
    if digits.startswith("86") and len(digits) == 13:      # +86-13xxxxxxxxx -> 13xxxxxxxxx
        digits = digits[2:]
    return digits if len(digits) == 11 else None


def norm(v: object) -> str | None:
    """NaN / empty / 'nan' -> None, else a stripped string (drops a trailing '.0').

    Series.replace({'nan': None}) does NOT do this: pandas treats the None replacement as NaN,
    so the string 'nan' becomes a float NaN instead of a Python None, and a later `not value`
    test then misbehaves. Doing it element-wise with pd.isna() first avoids that trap.
    """
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def dsp(v: object) -> str:
    """Display helper: render a NaN cell as '—' instead of the literal 'nan'."""
    return "—" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def office_of(dept: object) -> str:
    """Extract the clinical sub-department from the hierarchical department string."""
    for part in str(dept).split(","):
        if SCOPE_KW in part:
            tail = part.split(SCOPE_KW, 1)[1].lstrip("-")
            return tail.split("-")[0] if tail else SCOPE_KW
    return SCOPE_KW


def recover_codes() -> dict[str, str]:
    """姓名 -> 工号, from drg.result_insim where the charset is already correct."""
    d = ddlio.query("SELECT DISTINCT doctor FROM result_insim WHERE doctor IS NOT NULL", "drg")
    out: dict[str, str] = {}
    for v in d["doctor"].dropna():
        if "|" in str(v):
            code, name = str(v).split("|", 1)
            out[name.strip()] = code.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    ap.add_argument("--dry", action="store_true", help="只预览不写库")
    args = ap.parse_args()

    d = read_phonebook(args.file)
    zh = d[d["部门名称"].astype(str).str.contains(SCOPE_KW, na=False)].copy()

    zh["doctor_phone"] = zh["手机号码"].map(clean_phone)
    zh["doctor_code"] = zh["员工工号"].map(norm)
    zh["doctor_name"] = zh["姓名"].map(norm)
    zh["office"] = zh["部门名称"].map(office_of)
    zh["title"] = zh["职位"].map(norm)

    rec = recover_codes()
    filled = 0
    for i, r in zh.iterrows():
        # pd.isna, not `not`: Series.map turns a None return back into float NaN,
        # and `not float('nan')` is False (NaN is truthy), which would skip recovery.
        if pd.isna(r["doctor_code"]) and r["doctor_name"] in rec:
            zh.at[i, "doctor_code"] = rec[r["doctor_name"]]
            filled += 1

    # 使用方定："没有工号的使用手机号代替作为工号"（工号不管）。照此补齐，sys_doctor 覆盖全部
    # 住院部临床，不因通讯录缺工号而漏人。s06 侧按姓名兜底，故代工号不影响实际匹配。
    filled_phone = 0
    for i, r in zh.iterrows():
        if pd.isna(r["doctor_code"]) and pd.notna(r["doctor_phone"]):
            zh.at[i, "doctor_code"] = r["doctor_phone"]
            filled_phone += 1

    no_phone = zh[zh["doctor_phone"].isna()]
    no_code = zh[zh["doctor_code"].isna()]
    nurses = zh[zh["title"].fillna("").str.contains(NURSE_KW)]
    out = zh.drop(no_phone.index).drop(no_code.index).drop(nurses.index).copy()
    out = out.drop_duplicates("doctor_phone").copy()

    out["active"] = 1
    out["source"] = "人事导出"
    out["updated_at"] = datetime.now()
    cols = ["doctor_phone", "doctor_code", "doctor_name", "office", "title",
            "active", "source", "updated_at"]
    out = out[cols].sort_values("doctor_code")

    print(f"通讯录总数 {len(d)}；住院部(部门含'{SCOPE_KW}') {len(zh)} 人")
    print(f"反查补齐工号 {filled} 人（姓名从 result_insim）；手机号代工号 {filled_phone} 人")
    print(f"将导入 {len(out)} 行 → sys_doctor")
    print()
    print("| 工号 | 姓名 | 手机号 | 职位 | 科室 |")
    print("|---|---|---|---|---|")
    for r in out.itertuples(index=False):
        print(f"| {r.doctor_code} | {r.doctor_name} | {r.doctor_phone} | {dsp(r.title)} | {r.office} |")

    if len(no_code) or len(no_phone):
        print()
        print("=== 跳过 ===")
        for _, r in no_code.iterrows():
            print(f"  缺工号: {r['doctor_name']} {r['doctor_phone']}（职位 {dsp(r['title'])}）")
        for _, r in no_phone.iterrows():
            print(f"  缺手机号: {r['doctor_name']} {r['doctor_code']}")

    if args.dry:
        print()
        print("[dry] 未写库")
        return 0

    ddlio.load_frame(out, "sys_doctor", truncate=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
