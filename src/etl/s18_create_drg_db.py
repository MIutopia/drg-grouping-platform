"""S18 Bootstrap and load the drg database.

Turns the off-line deliverables (dictionaries in src/config/dict, results in src/out) into
queryable tables, which is what Metabase, shadow running and the monthly inspection report
all depend on.

Modes:
    --check    probe permissions, instance environment and data availability (read-only)
    --create   run the DDL scripts under src/sql/ (creates the database and its objects)
    --load     populate dictionaries, parameters and results
    --status   report row counts per table
Requires DRG_ALLOW_DDL=1 for anything that writes.
Docs: docs/23-drg库设计与落地.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402
from dbio import query_df  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR, OUT_DIR, ROOT  # noqa: E402
from srcschema import col as src_col  # noqa: E402  源库列名映射（pandas 侧取物理列名）

DICT_DIR = CONFIG_DIR / "dict"
SQL_DIR = ROOT / "项目" / "sql"
DRG = "drg"

# policy_version per dictionary family, so revisions can coexist (year-end evidence).
PV_CATALOG = "某地区细分组目录2.0-2025版"
PV_NATIONAL = "CHS-DRG2.0-2025"
PV_TCM = "某地区医保局〔2025〕27号-2025版"
PV_INHOUSE = "院内规则-2026v1"
PV_RESULT = "2026Q2回测-v1"
PV_TCD = "GB/T 15657-2021"

SCRIPT_ORDER = ("01_create_database.sql", "02_dict.sql", "03_result.sql", "04_ops.sql",
                "07_policy_watch.sql", "08_platform.sql", "09_soft_delete_sys_user.sql",
                "09_tcd.sql", "10_ops_alert_severity.sql",
                "11_result_pnl_profit_columns.sql", "12_result_insim_index.sql")


# ------------------------------------------------------------------
def check() -> int:
    print("=== 1. 连接身份与建库权限 ===")
    d = query_df(
        "SELECT SUSER_SNAME() AS login_name, USER_NAME() AS db_user, "
        "IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin, "
        "IS_SRVROLEMEMBER('dbcreator') AS is_dbcreator, "
        "IS_SRVROLEMEMBER('securityadmin') AS is_securityadmin, "
        "HAS_PERMS_BY_NAME(NULL, NULL, 'CREATE ANY DATABASE') AS can_create_db")
    print(d.to_string(index=False))

    print("\n=== 2. 实例环境 ===")
    e = query_df("SELECT @@SERVERNAME AS srv, SERVERPROPERTY('ProductVersion') AS ver, "
                 "SERVERPROPERTY('Edition') AS edition, SERVERPROPERTY('Collation') AS coll")
    print(e.to_string(index=False))
    files = query_df("SELECT name, physical_name FROM sys.master_files "
                     "WHERE database_id = DB_ID()")
    print(files.to_string(index=False))

    print("\n=== 3. drg 库当前状态 ===")
    ex = query_df("SELECT name, create_date FROM sys.databases WHERE name = N'drg'")
    print(ex.to_string(index=False) if len(ex) else "  尚未创建")

    print("\n=== 4. 待装载数据是否齐备 ===")
    need = {
        "官方组目录": DICT_DIR / "drg_group_catalog.csv",
        "QY 伪组码": DICT_DIR / "drg_qy_groups.csv",
        "ADRG 入组条件": DICT_DIR / "drg_cond.csv",
        "中医规则": DICT_DIR / "tcm_advantage_rules.json",
        "收费项目属性": DICT_DIR / "cost_item_attribute.csv",
        "首页操作码映射": DICT_DIR / "homepage_op_class.csv",
        "结算返回": OUT_DIR / "settlement" / "settlement_returns.csv",
        "差异对表": OUT_DIR / "compare" / "prelim_joined.csv",
        "引擎输出": OUT_DIR / "adrg" / "adrg_predictions.csv",
        "编码助手提示": OUT_DIR / "coding" / "coding_findings.csv",
        "财务对账": OUT_DIR / "recon" / "recon_cases.csv",
    }
    for label, p in need.items():
        print(f"  {'OK ' if p.exists() else '缺 '} {label:<14} {p.name}")
    return 0


def create() -> int:
    print("=== 建库与建表 ===")
    log = []
    for name in SCRIPT_ORDER:
        p = SQL_DIR / name
        if not p.exists():
            print(f"    [!] 缺少脚本 {name}")
            continue
        log.append(ddlio.run_script(p, None if name.startswith("01") else DRG))
    # the ops tables only exist after 04, so the whole bootstrap is logged afterwards
    for info in log:
        _log_ddl(info)
    print(f"    DDL 执行 {len(log)} 个脚本，已留痕到 drg.ops_ddl_log")
    return 0


def _log_ddl(info: dict) -> None:
    try:
        ddlio._exec(  # noqa: SLF001  - intentional: ops log helper
            "INSERT INTO ops_ddl_log (script, sha256, batches, seconds) VALUES "
            f"(N'{info['script']}', '{info['sha256']}', {info['batches']}, {info['seconds']})",
            DRG)
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] DDL 留痕写入失败：{str(e)[:80]}")


# ------------------------------------------------------------------
# Loaders
# ------------------------------------------------------------------
def _csv(p: Path) -> pd.DataFrame:
    return pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")


# CSV is read as text, so numeric targets must be converted explicitly. Integer columns need
# this most: the source writes 51.0 and SQL Server refuses that for an int column.
INT_COLS = ("age", "actual_days", "list_upload_days", "days", "cases", "days_min", "min_ops",
            "order_no")
DEC_COLS = ("weight", "fee_rate", "org_coefficient", "drg_standard", "total_cost", "deductible",
            "first_self_pay", "account_mutual", "personal_account", "self_cost_all",
            "profit_loss", "pred_weight", "pred_std", "std_official", "std_counterfactual",
            "delta", "his_fee", "his_fee_pos", "diff", "diff_pos", "his_pnl", "zzl_ratio",
            "cmi", "tcm_share", "avg_cost", "avg_pnl", "std_cost", "tcm_ratio", "param_value")


def _numeric(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if c in INT_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce").round().astype("Int64")
        elif c in DEC_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _dedup(df: pd.DataFrame, subset: list[str], label: str) -> pd.DataFrame:
    """Guard against primary-key collisions in source data before it reaches the database."""
    before = len(df)
    d = df.drop_duplicates(subset=subset)
    if len(d) < before:
        print(f"    [dedup] {label}: 按 {subset} 去重 {before - len(d)} 行")
    return d


def load_dicts() -> None:
    print("=== 装载字典层 ===")
    L = ddlio.load_frame

    c = _csv(DICT_DIR / "drg_group_catalog.csv")
    L(_dedup(c, ["drg_code"], "dict_group_catalog")
      [["mdc", "drg_code", "drg_name", "drg_attr", "drg_type", "weight_23", "weight_1"]],
      "dict_group_catalog", policy_version=PV_CATALOG)

    q = _csv(DICT_DIR / "drg_qy_groups.csv")
    L(_dedup(q, ["qy_code"], "dict_qy_group")[["mdc", "qy_code"]],
      "dict_qy_group", policy_version=PV_CATALOG)

    cond = _csv(DICT_DIR / "drg_cond.csv")
    L(_dedup(cond, ["adrg", "kind", "code"], "dict_adrg_cond")
      [["mdc", "adrg", "kind", "code", "name"]], "dict_adrg_cond",
      policy_version=PV_NATIONAL)

    mz = DICT_DIR / "drg_mdcz.csv"
    if mz.exists():
        z = _csv(mz)
        L(_dedup(z, ["grp_name", "code"], "dict_mdcz_dx")[["mdc", "grp_name", "code", "name"]],
          "dict_mdcz_dx", policy_version=PV_NATIONAL)

    d = _csv(DICT_DIR / "drg_drg.csv")
    L(_dedup(d, ["drg"], "dict_drg")[["adrg", "drg", "drg_name"]], "dict_drg",
      policy_version=PV_NATIONAL)

    a = _csv(DICT_DIR / "drg_adrg.csv")
    L(_dedup(a, ["adrg"], "dict_adrg_name")[["adrg", "adrg_name"]], "dict_adrg_name",
      policy_version=PV_NATIONAL)

    m = _csv(DICT_DIR / "drg_main_dx.csv")
    L(_dedup(m, ["code", "mdc"], "dict_main_dx")[["mdc", "code", "name"]], "dict_main_dx",
      policy_version=PV_NATIONAL)

    sev = []
    for kind, fname in (("cc", "drg_cc.csv"), ("mcc", "drg_mcc.csv")):
        x = _csv(DICT_DIR / fname)
        x = x.assign(kind=kind)
        sev.append(x[["kind", "code", "name", "excl"]].rename(columns={"excl": "note"}))
    x = _csv(DICT_DIR / "drg_exclude.csv")
    sev.append(x[["code", "name"]].assign(kind="exclude", note=x["excl"] if "excl" in x
                                          else "")[["kind", "code", "name", "note"]])
    L(_dedup(pd.concat(sev, ignore_index=True), ["kind", "code"], "dict_severity"),
      "dict_severity", policy_version=PV_NATIONAL)

    # both kinds share one table, so they must be concatenated: loading them separately would
    # have the second DELETE wipe the first
    nomain = []
    for kind, fname in (("dx", "drg_no_main_dx.csv"), ("op", "drg_no_main_op.csv")):
        x = _csv(DICT_DIR / fname)
        nomain.append(x.assign(kind=kind)[["kind", "code", "name"]])
    L(_dedup(pd.concat(nomain, ignore_index=True), ["kind", "code"], "dict_no_main"),
      "dict_no_main", policy_version=PV_NATIONAL)

    # TCM advantage groups: the JSON is nested, so it is flattened into three tables
    rules = json.loads((DICT_DIR / "tcm_advantage_rules.json").read_text(encoding="utf-8"))
    g_rows, dx_rows, op_rows = [], [], []
    for code, gg in rules.items():
        g_rows.append({"grp_code": code, "grp_name": gg.get("name", ""),
                       "order_no": gg.get("order"), "days_min": gg.get("days"),
                       "op_mode": gg.get("op_mode"), "min_ops": gg.get("min_ops")})
        for dd in gg.get("dx", []):
            dx_rows.append({"grp_code": code, "dx_code": dd.get("code"),
                            "dx_name": dd.get("name", "")})
        for key, lst in gg.get("ops", {}).items():
            cls = key if key in ("0", "1", "2", "3", "4") else "0"
            for oo in lst:
                op_rows.append({"grp_code": code, "op_class": cls,
                                "op_code": oo.get("code"), "op_name": oo.get("name", "")})
    L(_dedup(pd.DataFrame(g_rows), ["grp_code"], "dict_tcm_group"),
      "dict_tcm_group", policy_version=PV_TCM)
    L(_dedup(pd.DataFrame(dx_rows), ["grp_code", "dx_code"], "dict_tcm_group_dx"),
      "dict_tcm_group_dx", policy_version=PV_TCM)
    L(_dedup(pd.DataFrame(op_rows), ["grp_code", "op_code"], "dict_tcm_group_op"),
      "dict_tcm_group_op", policy_version=PV_TCM)

    ca = _csv(DICT_DIR / "cost_item_attribute.csv")
    for src, tgt in (("is_tcm_catalog", "is_tcm_std"),):
        if src in ca.columns:
            ca = ca.rename(columns={src: tgt})
    ca = ca.assign(**{k: ca.get(k, "") for k in
                      ("is_tcm_std", "is_tcm_cq4", "is_tcm_cost04", "is_tcm_item")})
    L(ca[[src_col("cost_no"), src_col("cost_name"), src_col("sort_code"), src_col("sort_kind"), "op_class",
          "zzl_subject", "is_tcm_std", "is_tcm_cq4", "is_tcm_cost04", "is_tcm_item"]],
      "dict_cost_item_attr", policy_version=PV_INHOUSE)

    hp = _csv(DICT_DIR / "homepage_op_class.csv")
    hp = hp.rename(columns={"手术编码": "op_code", "手术名称": "op_name", "例数": "cases",
                            "性质": "kind", "匹配方式": "match_mode", "依据": "evidence"})
    L(hp[["op_code", "op_name", "op_class", "kind", "match_mode", "evidence", "cases"]],
      "dict_homepage_op_class", policy_version=PV_INHOUSE)

    from s14_coding_assistant import DRUG_HINTS  # noqa: PLC0415
    dh = pd.DataFrame([{"pattern": p, "dx_prefixes": "/".join(px), "label": lb,
                        "confidence": "中", "note": "线索，非编码依据"} for p, px, lb in DRUG_HINTS])
    L(dh, "dict_drug_hint", policy_version=PV_INHOUSE)

    tcd = OUT_DIR / "tcd" / "official_tcd_codes.csv"
    if tcd.exists():
        t = _csv(tcd).rename(columns={"tcd": "tcd_code"})
        L(_dedup(t, ["tcd_code"], "dict_tcd_map")[["source", "name", "tcd_code", "name_key"]],
          "dict_tcd_map", policy_version=PV_TCD)
    else:
        print("    [warn] 未找到 TCD 码表（先运行 s12_tcd_map.py）")


def load_params() -> None:
    print("=== 装载参数层 ===")
    from drg_engine import COEF, FEE_RATE  # noqa: PLC0415
    rows = [
        ("fee_rate", FEE_RATE, "元/权重", "某地区 DRG 费率，经 N 例与附件2费用反算确认",
         "某地区医保局〔2025〕26号"),
        ("org_coefficient", COEF, "—", "机构系数，S01 由 N 例反推 0.9200",
         "某地区医保局〔2025〕26号"),
        ("tcm_ratio_threshold", 0.6, "—", "中治率硬门槛，低于则中医组降级为内科组",
         "某地区医保局〔2025〕27号 二·结算清算"),
        ("list_upload_limit_days", 10, "天", "出院后上传结算清单时限",
         "某地区医保局〔2025〕26号"),
        ("high_rate_ratio", 5.0, "倍", "费用极高（前 5%）按项目付费参考阈值",
         "某地区医保局〔2025〕26号"),
        ("low_rate_ratio", 0.4, "倍", "费用极低（<40%）按项目付费参考阈值",
         "某地区医保局〔2025〕26号"),
        ("days_min_bone", 10, "天", "中医正骨术组住院天数门槛",
         "某地区医保局〔2025〕27号 附件2"),
        ("days_min_treat", 10, "天", "中医治疗组住院天数门槛",
         "某地区医保局〔2025〕27号 附件2"),
        ("risk_fund_pct", 5.0, "%", "区域预算预留 5% 作为 DRG 付费风险金，用于年终清算",
         "某地区医保局〔2025〕26号"),
        # 单议数量上限随机构等级：三级 5‰ / 二级 3‰ / 一级 1‰。本单位为二级中医医院，取 3‰。
        ("single_case_permille", 3.0, "‰", "特病单议数量上限（三级 5‰ / 二级 3‰ / 一级 1‰）"
         "；本单位为二级中医医院，按二级 3‰", "某地区医保局〔2025〕26号"),
    ]
    df = pd.DataFrame(rows, columns=["param_key", "param_value", "unit", "note", "policy_basis"])
    ddlio.load_frame(df, "param_settlement", policy_version="某地区医保局〔2025〕26/27号-2025版")


def load_results() -> None:
    print("=== 装载结果层 ===")
    L = ddlio.load_frame

    s = _csv(OUT_DIR / "settlement" / "settlement_returns.csv")
    keep = ["settle_id", "org_settle_id", "medical_no", "biz_no", "gender", "age",
            "insurance_type", "medical_category", "office_name", "refund_flag", "drg_code",
            "drg_name", "weight", "fee_rate", "org_coefficient", "drg_standard", "settle_date",
            "in_time", "out_time", "actual_days", "total_cost", "deductible", "first_self_pay",
            "account_mutual", "personal_account", "self_cost_all", "profit_loss",
            "main_diag_code", "main_op_code", "other_diag_codes", "other_op_codes",
            "list_upload_days", "settle_ym", "source_file", "source_sheet", "row_uid"]
    s = _dedup(s, ["settle_id"], "result_settlement_return")
    s = s[[c for c in keep if c in s.columns]]
    for col in ("settle_date", "in_time", "out_time"):
        if col in s.columns:
            s[col] = pd.to_datetime(s[col], errors="coerce")
    L(_numeric(s), "result_settlement_return", policy_version=PV_RESULT)

    j = _csv(OUT_DIR / "compare" / "prelim_joined.csv")
    p = _csv(OUT_DIR / "adrg" / "adrg_predictions.csv")
    p = p.rename(columns={"drg_code": "off_drg"})
    j = j.rename(columns={"drg_code": "grp_official", "drg_name": "off_name",
                          src_col("group_code"): "grp_vendor"})
    m = j.merge(p[["medical_no", "pred_drg", "pred_adrg", "path", "reason"]],
                on="medical_no", how="left")
    m = m.assign(
        adrg_official=m["grp_official"].str[:3],
        adrg_engine=m["pred_adrg"],
        agree_drg=(m["pred_drg"].fillna("") == m["grp_official"].fillna("")),
        agree_adrg=(m["pred_adrg"].fillna("") == m["grp_official"].str[:3].fillna("")),
        # the vendor column holds a full 4-digit code, so compare its ADRG part
        agree_adrg_vendor=(m["grp_vendor"].fillna("").str[:3]
                           == m["grp_official"].str[:3].fillna("")),
        official_tcm_group=m["grp_official"].str.endswith(("F", "R", "Z")),
        engine_tcm_group=m["pred_drg"].fillna("").str.endswith(("F", "R", "Z")),
    ).rename(columns={"grp_off": "adrg_off_x"})
    L(_numeric(_dedup(m, ["medical_no"], "result_compare_case")
               [["medical_no", "settle_id", "grp_official", "grp_vendor", "pred_drg",
                 "adrg_official", "adrg_engine", "agree_drg", "agree_adrg", "agree_adrg_vendor",
                 "path", "official_tcm_group", "engine_tcm_group", "drg_standard", "weight",
                 "total_cost", "profit_loss", "actual_days", "main_diag_code", "main_op_code",
                 "reason"]].rename(columns={"pred_drg": "grp_engine"})),
      "result_compare_case", policy_version=PV_RESULT)

    L(_numeric(_dedup(p, ["medical_no"], "result_engine_pred")
               [["medical_no", "pred_drg", "pred_adrg", "pred_sev", "pred_weight", "pred_std",
                 "path", "qy", "reason"]].rename(columns={"qy": "qy_code"})),
      "result_engine_pred", policy_version=PV_RESULT)

    f = _csv(OUT_DIR / "coding" / "coding_findings.csv")
    if len(f):
        L(f[["medical_no", "rule", "confidence", "finding", "basis", "impact"]],
          "result_coding_finding", policy_version=PV_RESULT)

    cf = _csv(OUT_DIR / "coding" / "counterfactual_tcm_loss.csv")
    if len(cf):
        cf = cf.rename(columns={"name": "group_name", "官方支付标准": "std_official",
                                "可达支付标准": "std_counterfactual", "差额": "delta"})
        L(_numeric(_dedup(cf, ["medical_no"], "result_coding_counterfactual")
                   [["medical_no", "official_drg", "counterfactual_drg", "group_name",
                     "std_official", "std_counterfactual", "delta"]]),
          "result_coding_counterfactual", policy_version=PV_RESULT)

    r = _csv(OUT_DIR / "recon" / "recon_cases.csv")
    r = r.assign(his_fee_pos="", diff_pos="") if "his_fee_pos" not in r.columns else r
    L(_numeric(_dedup(r, ["medical_no"], "result_recon_case")
               [["medical_no", "drg_code", "total_cost", "his_fee", "diff", "diff_type",
                 "drg_standard", "profit_loss", "weight", "fee_rate", "zzl_ratio"]]),
      "result_recon_case", policy_version=PV_RESULT)

    _load_insim()
    _load_pnl()


INSIM_MAP = {src_col("hospital_no"): "hospital_no", "dx": "main_dx", "dx_name": "main_dx_name",
             "ops": "ops_text", "中治率": "tcm_ratio", "中治率判定": "tcm_ratio_note",
             "分组路径": "path_basis", "L1_当前预分组": "line1_group", "group": "drg_code",
             "weight": "weight", "standard": "std_cost", "fee": "fee_to_date",
             "delta": "delta", "L2_入组差距": "line2_gap", "L3_费用预判": "line3_cost",
             "L4_优化空间": "line4_advice"}


def _load_insim() -> None:
    """Load the latest in-hospital simulator snapshot, one row per patient per day."""
    sub = OUT_DIR / "insim"
    files = sorted(sub.glob("insim_*.csv"))
    if not files:
        print("    [warn] 未找到在院模拟器产出（先运行 s06_insim.py）")
        return
    frames = []
    for f in files:
        d = _csv(f).rename(columns=INSIM_MAP)
        d["snapshot_date"] = f.stem.replace("insim_", "")
        for c in ("hospital_no", "doctor", "doctor_phone", "office", "days", "main_dx",
                  "main_dx_name", "ops_text", "op_classes", "tcm_ratio", "tcm_ratio_note",
                  "path_basis", "line1_group", "line2_gap", "line3_cost", "line4_advice",
                  "drg_code", "weight", "std_cost", "fee_to_date", "delta"):
            if c not in d.columns:
                d[c] = None
        frames.append(d[["snapshot_date", "hospital_no", "doctor", "doctor_phone", "office",
                         "days", "main_dx", "main_dx_name", "ops_text", "op_classes",
                         "tcm_ratio", "tcm_ratio_note", "path_basis", "line1_group",
                         "line2_gap", "line3_cost", "line4_advice", "drg_code", "weight",
                         "std_cost", "fee_to_date", "delta"]])
    allf = pd.concat(frames, ignore_index=True).fillna("")
    allf["snapshot_date"] = pd.to_datetime(allf["snapshot_date"], errors="coerce").dt.date
    ddlio.load_frame(_numeric(_dedup(allf, ["snapshot_date", "hospital_no"], "result_insim")),
                     "result_insim", policy_version=PV_INHOUSE)


PNL_COLS = ["level", "period", "key_code", "key_name", "cases", "total_cost", "std_cost",
            "profit_loss", "profit_formula", "profit_adjust", "profit_basis",
            "cmi", "tcm_share", "avg_cost", "avg_pnl", "is_tcm_group"]
CN_MAP = {"例数": "cases", "总费用": "total_cost", "支付标准": "std_cost", "盈亏": "profit_loss",
          "中医组占比": "tcm_share", "例均费用": "avg_cost", "例均盈亏": "avg_pnl",
          "组名": "key_name", "权重": "cmi", "中医优势病组": "is_tcm_group", "month": "period",
          # docs/35 D5: profit_loss blends the official field (which zeroes high/low-rate cases)
          # with the formula for months that lack it, so the aggregate legitimately does NOT equal
          # std_cost - total_cost. Carry both so the difference is explicit and checkable.
          "盈亏公式值": "profit_formula", "盈亏调整": "profit_adjust", "盈亏口径": "profit_basis"}


def _pnl_frame(df: pd.DataFrame, level: str, key_code_col: str,
               key_name_col: str | None = None) -> pd.DataFrame:
    """Normalise one board export into the unified result_pnl shape."""
    d = df.rename(columns=CN_MAP)
    d["level"] = level
    d["key_code"] = d[key_code_col] if key_code_col in d.columns else ""
    d["key_name"] = (d[key_name_col] if key_name_col and key_name_col in d.columns
                     else d["key_code"])
    for c in PNL_COLS:
        if c not in d.columns:
            d[c] = None
    return d[PNL_COLS]


def _load_pnl() -> None:
    frames = []
    b = OUT_DIR / "board"
    h = _csv(b / "board_hospital.csv")
    if len(h):
        frames.append(_pnl_frame(h, "hospital", "period"))
    d = _csv(b / "board_drg.csv")
    if len(d):
        frames.append(_pnl_frame(d, "drg", "drg_code", "key_name"))
    o = _csv(b / "board_office.csv")
    if len(o):
        frames.append(_pnl_frame(o, "office", "office_name"))
    if not frames:
        print("    [warn] 未找到看板产出")
        return
    allf = pd.concat(frames, ignore_index=True).fillna("")
    allf["period"] = allf["period"].replace("", "2026-03~05")
    allf["is_tcm_group"] = allf["is_tcm_group"].map(
        lambda v: 1 if str(v).strip() in ("是", "True", "1") else 0)
    ddlio.load_frame(_numeric(allf[PNL_COLS]), "result_pnl", policy_version=PV_RESULT)


def status() -> int:
    d = ddlio.query(
        "SELECT t.name AS 表名, SUM(p.rows) AS 行数 FROM sys.tables t "
        "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1) "
        "GROUP BY t.name ORDER BY t.name", DRG)
    print(d.to_string(index=False))
    print(f"\n合计 {len(d)} 张表，{int(d['行数'].sum()) if len(d) else 0:,} 行")
    return 0


def schema_check() -> int:
    """Compare every dictionary CSV's real column widths against the target columns."""
    tables = {
        "dict_group_catalog": ["mdc", "drg_code", "drg_name", "drg_attr", "drg_type",
                               "weight_23", "weight_1"],
        "dict_qy_group": ["mdc", "qy_code"],
        "dict_adrg_cond": ["mdc", "adrg", "kind", "code", "name"],
        "dict_drg": ["adrg", "drg", "drg_name"],
        "dict_adrg_name": ["adrg", "adrg_name"],
        "dict_main_dx": ["mdc", "code", "name"],
        "dict_severity": ["kind", "code", "name", "note"],
        "dict_no_main": ["kind", "code", "name"],
        "dict_cost_item_attr": [src_col("cost_no"), src_col("cost_name"), src_col("sort_code"), src_col("sort_kind"),
                                "op_class", "zzl_subject"],
        "dict_homepage_op_class": ["op_code", "op_name", "op_class", "kind", "match_mode",
                                   "evidence", "cases"],
    }
    meta = ddlio.query(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH AS len "
        "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION", DRG)
    src = {
        "dict_group_catalog": "drg_group_catalog.csv",
        "dict_qy_group": "drg_qy_groups.csv",
        "dict_adrg_cond": "drg_cond.csv",
        "dict_drg": "drg_drg.csv",
        "dict_adrg_name": "drg_adrg.csv",
        "dict_main_dx": "drg_main_dx.csv",
        "dict_cost_item_attr": "cost_item_attribute.csv",
        "dict_homepage_op_class": "homepage_op_class.csv",
    }
    print("=== 源数据实际长度 vs 目标列宽 ===")
    for tbl, cols in tables.items():
        f = src.get(tbl)
        if not f:
            continue
        x = _csv(DICT_DIR / f)
        m = meta[meta["TABLE_NAME"] == tbl]
        for c in cols:
            if c not in x.columns:
                print(f"  [!] {tbl}.{c} 源列缺失")
                continue
            actual = int(x[c].astype(str).str.len().max()) if len(x) else 0
            row = m[m["COLUMN_NAME"] == c]
            width = int(row["len"].iloc[0]) if len(row) and pd.notna(row["len"].iloc[0]) else None
            flag = "" if width is None or actual <= width // (2 if row["DATA_TYPE"].iloc[0]
                                                              == "nvarchar" else 1) else "  <<< 超长"
            print(f"  {tbl}.{c:<18} 源 {actual:>4}  目标 {str(width):>6}"
                  f"  {row['DATA_TYPE'].iloc[0] if len(row) else '?'}{flag}")
    return 0


def verify() -> int:
    """Recompute the headline metrics straight from the drg database.

    Every SQL string stays ASCII-only: Chinese literals sent through this driver come back
    mangled (the data itself is fine), so labels are attached in pandas instead.
    """
    def q(sql: str) -> pd.DataFrame:
        return ddlio.query(sql, DRG)

    print("=== 1. 关键指标（从库里重算，用于与报告互证）===")
    r = q("SELECT COUNT(1) AS n, SUM(CAST(agree_adrg AS int)) AS adrg_ok, "
          "SUM(CAST(agree_drg AS int)) AS drg_ok, "
          "SUM(CAST(agree_adrg_vendor AS int)) AS vendor_ok FROM result_compare_case").iloc[0]
    n = int(r["n"])
    m = pd.DataFrame([
        ["回测例数", f"{n}"],
        ["ADRG（前 3 位）一致", f"{int(r['adrg_ok'])}（{int(r['adrg_ok']) / n:.1%}）"],
        ["四位码完全一致", f"{int(r['drg_ok'])}（{int(r['drg_ok']) / n:.1%}）"],
        ["既有引擎 ADRG 一致", f"{int(r['vendor_ok'])}（{int(r['vendor_ok']) / n:.1%}）"],
    ], columns=["指标", "值"])
    print(m.to_string(index=False))

    print("\n=== 2. 分组路径分布 ===")
    p = q("SELECT path, COUNT(1) AS n FROM result_engine_pred GROUP BY path ORDER BY COUNT(1) DESC")
    p = p.rename(columns={"path": "路径", "n": "例数"})
    print(p.to_string(index=False))

    print("\n=== 3. 编码助手：疑似操作漏编损失（同 ADRG 档位）===")
    c = q("SELECT COUNT(1) AS n, SUM(delta) AS total, MIN(delta) AS mn, MAX(delta) AS mx "
          "FROM result_coding_counterfactual").iloc[0]
    print(f"  例数 {int(c['n'])}　合计差额 {float(c['total']):,.2f} 元"
          f"　单例 {float(c['mn']):,.2f} ~ {float(c['mx']):,.2f}")

    print("\n=== 4. 字典规模 ===")
    tables = [("官方组目录", "dict_group_catalog"), ("QY 伪组码", "dict_qy_group"),
              ("ADRG 入组条件", "dict_adrg_cond"), ("ADRG 名称", "dict_adrg_name"),
              ("ADRG→DRG", "dict_drg"), ("主诊表", "dict_main_dx"),
              ("CC/MCC/排除", "dict_severity"), ("不作主诊/主手术", "dict_no_main"),
              ("中医组规则", "dict_tcm_group"), ("中医组诊断", "dict_tcm_group_dx"),
              ("中医组操作", "dict_tcm_group_op"), ("收费项目属性", "dict_cost_item_attr"),
              ("首页操作码映射", "dict_homepage_op_class"), ("MDCZ 诊断清单", "dict_mdcz_dx"),
              ("特征用药线索", "dict_drug_hint"), ("结算参数", "param_settlement")]
    rows = []
    for label, t in tables:
        c = q(f"SELECT COUNT(1) AS n FROM {t}").iloc[0]["n"]
        rows.append({"字典": label, "表": t, "行数": int(c)})
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== 5. 盈亏看板（病组层 Top 5）===")
    b = q("SELECT TOP 5 key_code, key_name, cases, profit_loss, is_tcm_group FROM result_pnl "
          "WHERE level = 'drg' ORDER BY profit_loss DESC")
    b = b.rename(columns={"key_code": "组码", "key_name": "组名", "cases": "例数",
                          "profit_loss": "盈亏", "is_tcm_group": "中医组"})
    print(b.to_string(index=False))

    print("\n=== 6. 建库与迁移留痕 ===")
    d = q("SELECT script, sha256, batches, executed_at FROM ops_ddl_log ORDER BY id DESC")
    d = d.rename(columns={"script": "脚本", "sha256": "校验和", "batches": "批数",
                          "executed_at": "执行时间"})
    print(d.to_string(index=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--verify", action="store_true", help="从 drg 库重算关键指标")
    ap.add_argument("--schema", action="store_true", help="列宽核对")
    ap.add_argument("--sql", default=None, help="执行 src/sql 下的单个脚本（迁移用）")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.schema:
        return schema_check()
    if a.sql:
        _log_ddl(ddlio.run_script(SQL_DIR / a.sql, DRG))
        return 0
    if a.verify:
        return verify()
    if a.create:
        return create()
    if a.load:
        load_dicts()
        load_params()
        load_results()
        return 0
    if a.status:
        return status()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
