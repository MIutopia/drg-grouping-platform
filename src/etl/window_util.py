"""结算回测窗口的自适应推导（S02 对表 / S11 引擎回测共用）。

背景：s02 与 s11 各自写死 `OUT_FROM, OUT_TO = "2026-02-20", "2026-06-01"`。
6/7 月结算返回导入后，旧窗口会把新月份整段挡在 HIS 取数之外 ——
s02 丢掉对表样本、s11 取不到诊断/手术导致引擎对新月份一例都产不出预测
（表现为"对表一致率"被无预测的行稀释）。故改为随导入数据自适应。

两个坑（都踩过）：
1. 出院时间 out_time 只有 3 月表有值，结算日期 settle_date 从 4 月起才有值，
   两列互补，只取其一会漏掉另一批月份；
2. settle_date 经 s01 还原后带时区，与无时区的 out_time 比较会抛
   "Cannot compare tz-naive and tz-aware timestamps"；剥时区要用
   tz_localize(None)（保留本地墙钟）而非 tz_convert(None)（会拨回 UTC）。
"""

from __future__ import annotations

import pandas as pd

OUT_FROM_DEFAULT = "2026-02-20"
OUT_FALLBACK_TO = "2026-08-01"


def _naive(off: pd.DataFrame, col: str) -> pd.Series | None:
    if col not in off.columns:
        return None
    s = pd.to_datetime(off[col], errors="coerce")
    try:
        s = s.dt.tz_localize(None)
    except TypeError:
        pass                                   # 本就无时区
    return s


def window_from(off: pd.DataFrame) -> tuple[str, str]:
    """按结算返回自适应窗口：[最早月 1 日, 最晚月次月 1 日)。"""
    cols = [s for s in (_naive(off, "out_time"), _naive(off, "settle_date")) if s is not None]
    if not cols:
        return OUT_FROM_DEFAULT, OUT_FALLBACK_TO
    # 按列取极值再合并：逐行 min/max 在两列 dtype 不一致时会抛
    # "'<=' not supported between instances of 'float' and 'Timestamp'"
    mins = [c.min() for c in cols if c.notna().any()]
    maxs = [c.max() for c in cols if c.notna().any()]
    if not mins:
        return OUT_FROM_DEFAULT, OUT_FALLBACK_TO
    lo, hi = min(mins), max(maxs)
    if pd.isna(lo) or pd.isna(hi):
        return OUT_FROM_DEFAULT, OUT_FALLBACK_TO
    start = lo.to_period("M").to_timestamp()
    end = (hi.to_period("M") + 1).to_timestamp()
    from_ = OUT_FROM_DEFAULT if start < pd.Timestamp(OUT_FROM_DEFAULT) else start.strftime("%Y-%m-%d")
    return from_, end.strftime("%Y-%m-%d")
