"""源库（HIS）表名与列名的逻辑映射层。

代码里不写物理名，只写自解释令牌：

* 表名 `源表(key)`（如 `源表(admission)`）——物理名在 `src/config/source_schema.json`
* 列名 `列(key)`（如 `列(hospital_no)`）——物理名在 `src/config/source_columns.json`

展开只发生在 **dbio 这一处**——所有对源库的查询都经过它，所以换现场只改配置，不动代码。
pandas 侧的列访问用 `col(key)` 取物理名：**查询结果的列名仍是物理列名**，因此
CSV 产物与 drg 装载的契约完全不变（这点很关键，否则会牵动所有下游）。

为什么用中文令牌而不是 __SRC_x__：同一个名字也会出现在**注释与报告文本**里（"数据源：
`源表(advice_long)`"），令牌必须在那里也可读；而 dbio 在拼 SQL 前会把它换成物理名，
SQL 侧永远看不到令牌。

失败要响：配置里缺键 → 抛 RuntimeError 并列出可用键，绝不静默拼出空名去查库。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "config"))
from settings import CONFIG_DIR  # noqa: E402

SCHEMA_JSON = CONFIG_DIR / "source_schema.json"
COLUMN_JSON = CONFIG_DIR / "source_columns.json"
TOKEN = re.compile(r"(源表|列)\(([a-z0-9_]+)\)")

_CACHE: dict[str, str] | None = None
_CCOLS: dict[str, str] | None = None


def _load(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RuntimeError(f"缺少源库契约配置：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in data.items() if not k.startswith("_")}


def mapping(*, reload: bool = False) -> dict[str, str]:
    """逻辑键 -> 物理表名（读配置，进程内缓存）。"""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE
    _CACHE = _load(SCHEMA_JSON)
    return _CACHE


def colmap(*, reload: bool = False) -> dict[str, str]:
    """逻辑键 -> 物理列名（读配置，进程内缓存）。"""
    global _CCOLS
    if _CCOLS is not None and not reload:
        return _CCOLS
    _CCOLS = _load(COLUMN_JSON)
    return _CCOLS


def physical(key: str) -> str:
    """取单个物理表名（供需要裸表名的调用点用，如 list_tables/表是否存在）。"""
    m = mapping()
    if key not in m:
        raise RuntimeError(f"源库表名配置缺少键 {key!r}；可用键：{sorted(m)}")
    return m[key]


def col(key: str) -> str:
    """取物理列名（pandas 侧列访问用）。

    查询结果集的列名仍是**物理列名**，因此只有"代码里引用哪一列"走映射，
    CSV 产物与 drg 装载契约完全不变。
    """
    m = colmap()
    if key not in m:
        raise RuntimeError(f"源库列名配置缺少键 {key!r}；可用键：{sorted(m)}")
    return m[key]


def expand(text: str) -> str:
    """把文本里的 源表(key) / 列(key) 令牌展开为物理名；未知键立即报错。"""
    if not text or ("源表(" not in text and "列(" not in text):
        return text

    def sub(m: re.Match[str]) -> str:
        kind, key = m.group(1), m.group(2)
        table = (mapping() if kind == "源表" else colmap()).get(key)
        if table is None:
            where = sorted(mapping() if kind == "源表" else colmap())
            raise RuntimeError(
                f"源库{'表名' if kind == '源表' else '列名'}配置缺少键 {key!r}"
                f"（出现在：{text[:80]}…）；可用键：{where}")
        return table

    return TOKEN.sub(sub, text)


def keys() -> list[str]:
    return sorted(mapping())


def col_keys() -> list[str]:
    return sorted(colmap())


def audit_sources(root: Path | None = None) -> list[str]:
    """自检：`src/**.py` 里不得再出现配置中的**物理表名**（只允许 源表(key)）。

    这是抽象是否被绕过的常驻闸门：新增作业时若直接写物理表名，`s00_env_check` 会报出来。
    """
    from settings import ROOT as _ROOT  # noqa: PLC0415

    base = Path(root or _ROOT) / "项目"
    names = [p for p in mapping().values() if p]
    col_names = [c for c in colmap().values() if c]
    # 我方自有/派生字段（非源库列），形态巧合但不应被判定为"未配置的物理列名"。
    OWN = {"n_bytes", "n_files", "n_col", "n_groups", "n_pred", "n_rows", "n_cases",
           "n_std", "n_calc", "n_hit"}
    any_col = re.compile(r"\b[sndf]_[a-z0-9_]{3,}\b")
    # 不变式：源库/既有系统/备份库的**任何**物理表名都不许出现在作业代码里，
    # 而不只是"配置里列出的那些"——枚举会漏（首轮就漏了文档里的 源表(account) 等）。
    any_source = re.compile(r"\bHIS_[A-Za-z0-9_]+|\bDrg_[A-Za-z0-9_]+"
                            r"|源表(源表(病案main))[A-Za-z0-9_]*|med_record_[A-Za-z0-9_]+"
                            r"|hospitalisation_[A-Za-z0-9_]+")
    bad: list[str] = []
    # 两种令牌都要先抹掉：令牌里写着逻辑键，否则会被当成物理名误判
    token = re.compile(r"(?:源表|列)\([a-z0-9_]+\)")
    for f in sorted(base.rglob("*.py")):
        rel = f.relative_to(Path(root or _ROOT)).as_posix()
        if (rel.startswith("src/testenv/") or rel.startswith("src/out/")
                or f.name in {"srcschema.py", "s36_package_release.py",
                              "s37_publish_export.py"}):
            continue        # 测试夹具、可再生产物目录与打包/发布工具不参与自检
        text = f.read_text(encoding="utf-8", errors="ignore")
        # 先把令牌本身抹掉，避免把逻辑键（如 源表(病案main)）误判成物理名
        text = token.sub(" ", text)
        for name in names:
            # 词边界而非子串：列(money) 不该因为 列(money_sortcode_) 而被判为残留
            if re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
                bad.append(f"{rel} 出现物理表名 {name}（应写 源表({_key_of(name)})）")
        for m in any_source.finditer(text):
            if m.group(0) not in names:
                bad.append(f"{rel} 出现未配置的物理表名 {m.group(0)}"
                           f"（应写 源表(key) 并补进 config/source_schema.json）")
        for name in col_names:
            if re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
                bad.append(f"{rel} 出现物理列名 {name}"
                           f"（应写 列({_col_key_of(name)}) 或 col(\"{_col_key_of(name)}\")）")
        # 泛化检查只看** SQL 上下文**：列(off) / n_hit 之类是我方计数变量，形似列名但出现
        # 在赋值语句里；按"是否出现在 SQL 语句中"判定，既不会放过真列名，也不会误报变量。
        for m in any_col.finditer(text):
            tok = m.group(0)
            if tok in col_names or tok in OWN:
                continue
            if not re.search(rf"(?:SELECT|FROM|JOIN|WHERE|UNION|AND |OR |ON |GROUP BY|ORDER BY"
                             rf"|COUNT\(|SUM\(|AS |IN \()[^\\\n]*\b{re.escape(tok)}\b", text):
                continue
            bad.append(f"{rel} 出现未配置的物理列名 {tok}"
                       f"（应写 列(key)/col(key) 并补进 config/source_columns.json）")
    return bad


def _key_of(physical_name: str) -> str:
    for k, v in mapping().items():
        if v == physical_name:
            return k
    return "?"


def _col_key_of(physical_name: str) -> str:
    for k, v in colmap().items():
        if v == physical_name:
            return k
    return "?"
