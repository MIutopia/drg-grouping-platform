"""S20 Regression gate: threshold verdict, run-on-run delta and the rolling monthly series.

docs/24 turns an external policy change into a process: radar -> impact assessment -> versioned
update + regression -> traceable switch. Step 3 requires "run the full back-test and judge it
against the thresholds" to be a single executable action rather than a manual comparison.
This job is that action.

It adds three things the S11 report cannot give on its own:

  1. a PASS/FAIL verdict against the agreed bar (ADRG >= 96.0%, 4-digit >= 81.3%);
  2. run-over-run deltas, so a change is judged against the previous run rather than a constant;
  3. the rolling monthly series - the reverse-detection signal in docs/24 section 3: an
     unexpected month-on-month drop points at the payer having changed a rule that is only
     visible in the settlement results.

Grouping is deliberately NOT re-implemented: this calls s11_adrg.run_backtest(), the same
function the S11 report uses, so a regression verdict and a back-test report cannot disagree.
Docs: docs/24-政策变更与DRG规则调整应对机制.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddlio  # noqa: E402
from drg_engine import PATH_NONE, PATH_STD, PATH_TCM  # noqa: E402
from s11_adrg import run_backtest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import OUT_DIR  # noqa: E402

OUT_SUB = OUT_DIR / "regress"
DRG = "drg"
OWNER = "s20"
POLICY_VERSION = "2026Q2回测-v1"        # same basis as the S18 result layer

# The agreed bar (docs/24 section 2, step 3). Recorded per run in ops_regress_run rather than
# only asserted here, so a past verdict stays readable against the bar it was judged by: a
# grouping-scheme revision legitimately moves these, and history must not be silently re-judged.
# Re-settled 2026-09-24 to the 3-7 month baseline (N cases), WITH a safety margin below the
# achieved rates (96.9% / 88.1%): monthly 4-digit rates swing 83.5-92.0%, so a bar set exactly at
# the achieved level would fail on normal fluctuation. 0.965 / 0.870 keep ~0.4pp / ~1.1pp of
# headroom while still catching real regressions (the previous 0.960/0.813 would have let a
# ~6.8pp 4-digit regression pass silently).
TH_ADRG = 0.965
TH_DRG = 0.870

# docs/24 section 3: a month-on-month drop beyond this triggers a "payer changed something"
# probe. Expressed in percentage points, not percent.
DROP_LIMIT_PP = 5.0

# A run with fewer cases is not comparable with the N-case baseline, so it can never pass.
MIN_CASES = 500

VERDICT_PASS, VERDICT_WARN, VERDICT_FAIL = "pass", "warn", "fail"
SEV_HIGH, SEV_WARN = "high", "warn"


# ------------------------------------------------------------------
# Readings
# ------------------------------------------------------------------
def table_ready() -> bool:
    try:
        ddlio.query("SELECT TOP 1 run_id FROM ops_regress_run", DRG)
        return True
    except Exception:  # noqa: BLE001
        return False


def previous_run() -> dict | None:
    """The most recent stored run, or None on the very first run."""
    d = ddlio.query("SELECT TOP 1 run_id, run_at, cases, adrg_rate, drg_rate, verdict "
                    "FROM ops_regress_run ORDER BY run_id DESC", DRG)
    return None if d.empty else d.iloc[0].to_dict()


def vendor_rate() -> float | None:
    """Vendor-engine ADRG agreement, for the standing comparison in every report.

    Read from the result layer because it is a fixed historical figure rather than something
    this job recomputes: the vendor engine no longer runs (docs/06).
    """
    try:
        r = ddlio.query("SELECT COUNT(1) AS n, SUM(CAST(agree_adrg_vendor AS int)) AS ok "
                        "FROM result_compare_case", DRG).iloc[0]
        n = int(r["n"] or 0)
        return float(r["ok"]) / n if n else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------
def evaluable(pred: pd.DataFrame) -> pd.DataFrame:
    """只保留「引擎产得出预测」的病例。

    少数病例因源表缺诊断码（4 月起的官方返回表不含诊断列）而产不出 ADRG
    （N 例中有 10 例）。若把它们计入分母，等于当成"不一致"，会平白拉低
    一致率并可能误触发门槛 FAIL —— 与 manager_api 及 docs/23 的口径一致。
    """
    if "pred_adrg" not in pred.columns:
        return pred
    return pred[pred["pred_adrg"].notna()]


def evaluate(pred: pd.DataFrame, prev: dict | None) -> dict:
    ev = evaluable(pred)
    n = len(ev)
    a = int(ev["hit_adrg"].sum())
    d = int(ev["hit_drg"].sum())
    cur = {
        "cases": n,
        "total_cases": len(pred),
        "adrg_ok": a, "adrg_rate": a / n if n else 0.0,
        "drg_ok": d, "drg_rate": d / n if n else 0.0,
        "tcm_cases": int((ev["path"] == PATH_TCM).sum()),
        "std_cases": int((ev["path"] == PATH_STD).sum()),
        "none_cases": int((ev["path"] == PATH_NONE).sum()),
        "vendor_rate": vendor_rate(),
    }
    cur["delta_adrg"] = (cur["adrg_rate"] - float(prev["adrg_rate"])) * 100 if prev else None
    cur["delta_drg"] = (cur["drg_rate"] - float(prev["drg_rate"])) * 100 if prev else None
    return cur


def by_month(pred: pd.DataFrame) -> pd.DataFrame:
    """One row per settle month, plus the month-on-month change that the probe rule reads."""
    rows = []
    for m, g in evaluable(pred).groupby("settle_month", sort=True):
        if not m or str(m) == "NaT":
            continue
        rows.append({"dim": str(m), "cases": len(g),
                     "adrg_ok": int(g["hit_adrg"].sum()),
                     "adrg_rate": float(g["hit_adrg"].mean()),
                     "drg_ok": int(g["hit_drg"].sum()),
                     "drg_rate": float(g["hit_drg"].mean()),
                     "tcm_cases": int((g["path"] == PATH_TCM).sum()),
                     "std_cases": int((g["path"] == PATH_STD).sum()),
                     "none_cases": int((g["path"] == PATH_NONE).sum())})
    out = pd.DataFrame(rows)
    if len(out) > 1:
        out["mom_pp"] = out["adrg_rate"].diff() * 100
    else:
        out["mom_pp"] = None
    return out


# ------------------------------------------------------------------
# Judgement
# ------------------------------------------------------------------
def detect(cur: dict, prev: dict | None, series: pd.DataFrame) -> list[dict]:
    """Everything that should stop a release, or at least be read before one."""
    anoms: list[dict] = []

    if cur["cases"] < MIN_CASES:
        anoms.append({"level": SEV_HIGH, "item": "回归:样本量不足",
                      "detail": f"本次回测 {cur['cases']} 例，低于可比下限 {MIN_CASES} 例，"
                                "结果不具可比性"})

    if cur["adrg_rate"] < TH_ADRG:
        anoms.append({"level": SEV_HIGH, "item": "回归:ADRG 门槛未达",
                      "detail": f"ADRG 一致率 {cur['adrg_rate']:.1%}（{cur['adrg_ok']}/"
                                f"{cur['cases']}），低于门槛 {TH_ADRG:.1%}"})
    if cur["drg_rate"] < TH_DRG:
        anoms.append({"level": SEV_HIGH, "item": "回归:四位码门槛未达",
                      "detail": f"四位码一致率 {cur['drg_rate']:.1%}（{cur['drg_ok']}/"
                                f"{cur['cases']}），低于门槛 {TH_DRG:.1%}"})

    if prev is not None and cur["delta_adrg"] is not None:
        if cur["delta_adrg"] <= -DROP_LIMIT_PP:
            anoms.append({"level": SEV_HIGH, "item": "回归:一致率环比下降",
                          "detail": f"ADRG 一致率较上次运行（#{int(prev['run_id'])}）"
                                    f"下降 {abs(cur['delta_adrg']):.1f} 个百分点"
                                    f"（{float(prev['adrg_rate']):.1%} → "
                                    f"{cur['adrg_rate']:.1%}）"})

    for r in series.itertuples():
        if r.mom_pp is not None and pd.notna(r.mom_pp) and r.mom_pp <= -DROP_LIMIT_PP:
            anoms.append({"level": SEV_HIGH, "item": f"回归:月度一致率下降 {r.dim}",
                          "detail": f"{r.dim} ADRG 一致率 {r.adrg_rate:.1%}，较上月下降 "
                                    f"{abs(float(r.mom_pp)):.1f} 个百分点。若院内本期无字典/"
                                    "参数/编码变更，按 docs/24 第三节应排查医保侧规则调整"})
    return anoms


def verdict(cur: dict, anoms: list[dict]) -> str:
    if cur["adrg_rate"] < TH_ADRG or cur["drg_rate"] < TH_DRG or cur["cases"] < MIN_CASES:
        return VERDICT_FAIL
    return VERDICT_WARN if anoms else VERDICT_PASS


# ------------------------------------------------------------------
# Persistence
# ------------------------------------------------------------------
def persist(cur: dict, series: pd.DataFrame, vd: str, note: str) -> int:
    ins_run = ("INSERT INTO ops_regress_run (policy_version, cases, adrg_ok, adrg_rate, drg_ok, "
               "drg_rate, vendor_rate, th_adrg, th_drg, verdict, delta_adrg, delta_drg, note) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    ddlio.execute(ins_run, (POLICY_VERSION, cur["cases"], cur["adrg_ok"], cur["adrg_rate"],
                            cur["drg_ok"], cur["drg_rate"], cur["vendor_rate"], TH_ADRG, TH_DRG,
                            vd, cur["delta_adrg"], cur["delta_drg"], note), DRG)
    run_id = int(ddlio.query("SELECT MAX(run_id) AS id FROM ops_regress_run", DRG)
                 .iloc[0]["id"])

    ins_m = ("INSERT INTO ops_regress_metric (run_id, dim, cases, adrg_ok, adrg_rate, drg_ok, "
             "drg_rate, tcm_cases, std_cases, none_cases) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
    ddlio.execute(ins_m, (run_id, "ALL", cur["cases"], cur["adrg_ok"], cur["adrg_rate"],
                          cur["drg_ok"], cur["drg_rate"], cur["tcm_cases"], cur["std_cases"],
                          cur["none_cases"]), DRG)
    for r in series.itertuples():
        ddlio.execute(ins_m, (run_id, r.dim, int(r.cases), int(r.adrg_ok), float(r.adrg_rate),
                              int(r.drg_ok), float(r.drg_rate), int(r.tcm_cases),
                              int(r.std_cases), int(r.none_cases)), DRG)
    return run_id


def write_alerts(anoms: list[dict]) -> None:
    """Refresh this job's own open alerts.

    Scoped by [owner]: several jobs share ops_alert, so an unscoped
    "DELETE WHERE status='open'" would clear another job's live alerts.
    """
    ddlio._exec(f"DELETE FROM ops_alert WHERE status = 'open' AND [owner] = '{OWNER}'", DRG)  # noqa: SLF001
    for a in anoms:
        ddlio.insert_alert(a["level"], a["item"], a["detail"], OWNER, DRG)


def log_run(started: datetime, status: str, rows_out: int, message: str) -> None:
    ins = ("INSERT INTO ops_run_log (job_name, started_at, ended_at, status, rows_out, message, "
           "policy_version) VALUES (%s, %s, %s, %s, %s, %s, %s)")
    try:
        ddlio.execute(ins, ("s20_regress", started, datetime.now(), status, rows_out,
                            message, POLICY_VERSION), DRG)
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] ops_run_log 写入失败：{str(e)[:70]}")


# ------------------------------------------------------------------
# Report
# ------------------------------------------------------------------
def _pp(v: float | None) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—（首次运行）"
    return f"{v:+.1f} pp"


def report(cur: dict, prev: dict | None, series: pd.DataFrame, anoms: list[dict],
           vd: str, run_id: int | None) -> str:
    n = cur["cases"]
    period = (f"{series['dim'].min()} ~ {series['dim'].max()}"
              if len(series) else "（无月份数据）")
    lines = [
        "# S20 回归门槛判定报告",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 本次运行：#{run_id if run_id else '（dry-run 未入库）'}　判定：**{vd.upper()}**",
        f"- 判据：回测集 {period} 官方结算返回；分组逻辑与 S11 报告**同一函数**"
        "（`s11_adrg.run_backtest`），故回归判定与回测报告不可能互相矛盾",
        "",
        "## 一、门槛判定",
        "",
        "| 指标 | 本次 | 门槛 | 结果 |",
        "|---|---:|---:|---|",
        f"| ADRG（前 3 位）一致 | **{cur['adrg_rate']:.1%}**"
        f"（{cur['adrg_ok']:,}/{n:,}） | {TH_ADRG:.1%} | "
        f"{'达标' if cur['adrg_rate'] >= TH_ADRG else '**未达标**'} |",
        f"| 四位码完全一致 | **{cur['drg_rate']:.1%}**"
        f"（{cur['drg_ok']:,}/{n:,}） | {TH_DRG:.1%} | "
        f"{'达标' if cur['drg_rate'] >= TH_DRG else '**未达标**'} |",
        f"| 回测样本量（可评估 / 总） | {n:,} / {cur['total_cases']:,} | ≥{MIN_CASES:,} | "
        f"{'达标' if n >= MIN_CASES else '**未达标**'} |",
        "",
    ]
    if cur["vendor_rate"] is not None:
        lines += [f"> 对照：既有引擎 ADRG 一致率 {cur['vendor_rate']:.1%}，"
                  f"自建引擎 {cur['adrg_rate']:.1%}"
                  f"（高出 {(cur['adrg_rate'] - cur['vendor_rate']) * 100:.1f} 个百分点）。", ""]

    lines += ["## 二、与上次运行对比", ""]
    if prev is None:
        lines += ["- 库内无历史运行记录，本次为**首次**；环比自下次运行起可用。", ""]
    else:
        lines += [
            f"- 上次运行：**#{int(prev['run_id'])}**（{str(prev['run_at'])[:19]}），"
            f"判定 {str(prev['verdict']).upper()}",
            "",
            "| 指标 | 上次 | 本次 | 变化 | 阈值（>5pp 下降即预警） |",
            "|---|---:|---:|---:|---|",
            f"| ADRG 一致率 | {float(prev['adrg_rate']):.1%} | {cur['adrg_rate']:.1%} | "
            f"{_pp(cur['delta_adrg'])} | "
            f"{'**触发预警**' if (cur['delta_adrg'] or 0) <= -DROP_LIMIT_PP else '—'} |",
            f"| 四位码一致率 | {float(prev['drg_rate']):.1%} | {cur['drg_rate']:.1%} | "
            f"{_pp(cur['delta_drg'])} | "
            f"{'**触发预警**' if (cur['delta_drg'] or 0) <= -DROP_LIMIT_PP else '—'} |",
            "",
        ]

    lines += ["## 三、按月滚动一致率序列", "",
              "> 月份键取 `settle_date`（该列全部非空；源文件自带的 `settle_ym` 仅覆盖部分病例，"
              "不可用）。**环比下降超 5pp 即标注**——这是 docs/24 第三节的反向侦测信号。", ""]
    if len(series):
        lines += ["| 结算月份 | 例数 | ADRG 一致 | 四位码一致 | 中医组 | 标准组 | 未分组 | 环比 | 判定 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        for r in series.itertuples():
            mom = r.mom_pp
            flag = "**下降预警**" if (pd.notna(mom) and mom <= -DROP_LIMIT_PP) else "—"
            mom_txt = "—" if pd.isna(mom) else f"{float(mom):+.1f} pp"
            lines.append(f"| {r.dim} | {int(r.cases):,} | {r.adrg_rate:.1%} | {r.drg_rate:.1%} "
                         f"| {int(r.tcm_cases)} | {int(r.std_cases)} | {int(r.none_cases)} "
                         f"| {mom_txt} | {flag} |")
        lines.append("")
    else:
        lines += ["- 无可用月份数据。", ""]

    lines += ["## 四、异常清单", ""]
    if not anoms:
        lines += ["- 无。全部门槛达标、环比无异常。", ""]
    else:
        lines += ["| 级别 | 项 | 说明 |", "|---|---|---|"]
        for a in anoms:
            lines.append(f"| {'**异常**' if a['level'] == SEV_HIGH else '关注'} | {a['item']} "
                         f"| {a['detail']} |")
        lines.append("")

    lines += ["## 五、结论", "",
              f"- 判定：**{vd.upper()}**（门槛 {'达标' if vd != VERDICT_FAIL else '**未达标**'}，"
              f"异常 {len(anoms)} 项）",
              "- 变更后用法：改字典/参数 → `python src/etl/s20_regress.py` → 看本节判定；",
              "- 留痕：`drg.ops_regress_run`（每次运行一行）+ `drg.ops_regress_metric`"
              "（含按月序列）+ `drg.ops_alert`（异常）+ `drg.ops_run_log`（作业运行日志）。",
              ""]
    return "\n".join(lines)


# ------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只回测与出报告，不写库")
    ap.add_argument("--note", default="", help="本次运行的备注（写入 ops_regress_run.note）")
    a = ap.parse_args()
    started = datetime.now()
    OUT_SUB.mkdir(parents=True, exist_ok=True)

    ready = table_ready()
    if not ready and not a.dry:
        print("[!] drg.ops_regress_run 不存在。请先执行：")
        print("    $env:DRG_ALLOW_DDL=1; python src/etl/s18_create_drg_db.py --sql 04_ops.sql")
        return 2
    # ddlio gates every write connection behind DRG_ALLOW_DDL (the same convention as
    # s18 --load / s19), so a persist run needs it set. Checked up front so the failure is a
    # clear instruction rather than a traceback after several minutes of back-testing.
    if not a.dry and os.environ.get("DRG_ALLOW_DDL", "0") != "1":
        print("[!] 写库需要 DRG_ALLOW_DDL=1（ddlio 写入门禁，与 s18 --load 同）。用法：")
        print("    $env:DRG_ALLOW_DDL=1; python src/etl/s20_regress.py")
        print("    只查看判定不入库：python src/etl/s20_regress.py --dry")
        return 2

    print("[s20] 全量回测（调用 s11_adrg.run_backtest）...")
    pred, _eng = run_backtest()

    print("[s20] 门槛判定 / 环比 / 按月序列 ...")
    prev = previous_run() if ready else None
    cur = evaluate(pred, prev)
    series = by_month(pred)
    anoms = detect(cur, prev, series)
    vd = verdict(cur, anoms)

    series.to_csv(OUT_SUB / "regress_monthly.csv", index=False, encoding="utf-8-sig")

    if a.dry:
        md = report(cur, prev, series, anoms, vd, None)
        (OUT_SUB / "s20_regress_report.md").write_text(md, encoding="utf-8")
        print(md)
        print("[dry] 未写入 drg 库")
        return 0

    print("[3/3] 写入 drg 库 ...")
    run_id = persist(cur, series, vd, a.note)
    write_alerts(anoms)
    log_run(started, "ok", cur["cases"],
            f"verdict={vd} adrg={cur['adrg_rate']:.4f} drg={cur['drg_rate']:.4f} "
            f"anomalies={len(anoms)}")
    md = report(cur, prev, series, anoms, vd, run_id)
    (OUT_SUB / "s20_regress_report.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"    运行 #{run_id} 已入库；异常 {len(anoms)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
