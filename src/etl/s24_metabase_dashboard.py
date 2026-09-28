"""S24 Build the Metabase dashboards (在院患者 / 盈亏) via the Metabase API.

Uses the API key in src/config/metabase.json (gitignored) to create the native-query card and
dashboard directly, so 信息科 does not have to paste SQL by hand.

Modes:
    --probe    verify the API key and list databases (find the drg database id)
    (default)  create the 在院患者 card + dashboard
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CFG = ROOT / "项目" / "config" / "metabase.json"
SQL_INSIM = ROOT / "项目" / "metabase" / "在院患者_按医师.sql"


def load_cfg() -> tuple[str, str]:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    return cfg["base_url"].rstrip("/"), cfg["api_key"]


def api(base: str, key: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "X-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  [HTTP {e.code}] {method} {path} -> {body[:300]}")
        raise


def probe(base: str, key: str) -> int:
    try:
        u = api(base, key, "GET", "/api/user/current")
        print("认证 OK:", u.get("common_name") or u.get("email") or u.get("first_name") or "(未知)")
    except Exception as e:  # noqa: BLE001
        print("认证失败:", e)
        return 1
    dbs = api(base, key, "GET", "/api/database").get("data", [])
    print(f"数据库 {len(dbs)} 个：")
    for d in dbs:
        mark = "  <-- 可能是 drg" if "drg" in (d.get("name") or "").lower() else ""
        print(f"  id={d.get('id')}  name={d.get('name')}  engine={d.get('engine')}{mark}")
    return 0


def find_drg_db(base: str, key: str) -> int:
    dbs = api(base, key, "GET", "/api/database").get("data", [])
    for d in dbs:
        if "drg" in (d.get("name") or "").lower():
            return int(d["id"])
    raise SystemExit("未找到 drg 库：请确认 Metabase 中 DRG 连接的名称含 drg")


def _find(resp, name: str) -> dict | None:
    for r in _rows(resp):
        if r.get("name") == name:
            return r
    return None


def build(base: str, key: str, db_id: int) -> int:
    sql = SQL_INSIM.read_text(encoding="utf-8")
    query = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--")).strip()
    card_name = "在院患者 · 经管医师每日清单"
    dash_name = "在院患者 · 每日清单"

    # ---- create or update the card (always refresh its SQL) ----------
    card_payload = {
        "name": card_name, "display": "table",
        "dataset_query": {
            "type": "native",
            "native": {
                "query": query,
                "template-tags": {
                    "doctor": {"type": "text", "name": "doctor", "display-name": "经管医师"},
                    "office": {"type": "text", "name": "office", "display-name": "科室"},
                },
            },
            "database": db_id,
        },
        "visualization_settings": {},
    }
    card = _find(api(base, key, "GET", "/api/card"), card_name)
    if card is None:
        card = api(base, key, "POST", "/api/card", card_payload)
        print("卡片新建：", end="")
    else:
        card = api(base, key, "PUT", f"/api/card/{int(card['id'])}", card_payload)
        print("卡片更新：", end="")
    card_id = int(card["id"])
    print(f"id={card_id}  {card.get('name')}")

    # ---- find or create the dashboard ---------------------------------
    dash = _find(api(base, key, "GET", "/api/dashboard"), dash_name)
    if dash is None:
        dash = api(base, key, "POST", "/api/dashboard", {
            "name": dash_name, "description": "未出院患者四行预分组（s06 每日快照，经管医师视角）",
        })
        print("看板新建：", end="")
    else:
        print("看板复用：", end="")
    dash_id = int(dash["id"])
    print(f"id={dash_id}  {dash.get('name')}")

    # ---- link card -> dashboard (PUT dashcards, not POST .../cards) ----
    api(base, key, "PUT", f"/api/dashboard/{dash_id}", {
        "dashcards": [{
            "id": -1, "card_id": card_id,
            "row": 0, "col": 0, "size_x": 24, "size_y": 6,
            "parameter_mappings": [], "visualization_settings": {},
        }],
    })
    print(f"已把卡片 {card_id} 挂到看板 {dash_id}")
    print(f"访问地址：{base}/dashboard/{dash_id}")
    return 0


def _rows(resp) -> list:
    if isinstance(resp, list):
        return resp
    return resp.get("data", []) if isinstance(resp, dict) else []


def list_assets(base: str, key: str) -> int:
    print("=== 看板 ===")
    for d in _rows(api(base, key, "GET", "/api/dashboard")):
        print(f"  dashboard id={d.get('id')}  {d.get('name')}")
    print("=== 卡片（最近 10）===")
    for c in _rows(api(base, key, "GET", "/api/card"))[:10]:
        print(f"  card id={c.get('id')}  {c.get('name')}")
    return 0


def run_card(base: str, key: str, card_id: int) -> int:
    resp = api(base, key, "POST", f"/api/card/{card_id}/query", {})
    data = resp.get("data", {})
    rows = data.get("rows", [])
    cols = [c.get("name") for c in data.get("cols", [])]
    print(f"卡片 {card_id} 查询成功：{len(rows)} 行 x {len(cols)} 列")
    if cols:
        print("列：", cols)
    if rows:
        print("首行：", rows[0])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="验证 key 并列出数据库")
    ap.add_argument("--list", action="store_true", help="列出现有看板与卡片")
    ap.add_argument("--run", type=int, default=None, help="运行指定卡片的查询以验证")
    a = ap.parse_args()
    base, key = load_cfg()
    if a.probe:
        return probe(base, key)
    if a.list:
        return list_assets(base, key)
    if a.run is not None:
        return run_card(base, key, a.run)
    db_id = find_drg_db(base, key)
    print(f"drg 库 database id = {db_id}")
    return build(base, key, db_id)


if __name__ == "__main__":
    raise SystemExit(main())
