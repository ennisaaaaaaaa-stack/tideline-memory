#!/usr/bin/env python3
"""v2.9 view_state column 夹具（视线记录层）：amend 时记下「改前最后视线」。

背景（R97 独处时间草稿，SpaceChild 的信启发的对照）：
  amendments 表记 what/why/when，不记视线——R94 覆盖事故根因是
  「看着自己的恢复稿写，没看原档」。「verified twice」是关于程序的主张
  而程序本身可能是错的；读侧 attention_log 54 万行已覆盖，写侧缺视线。

验收点（spec: ~/drafts/view-state-column-spec-v0.md）：
  V1 列存在：amendments.view_state（ALTER 幂等）
  V2 amend 自动填视线：调用前该 nid 最近一次被照亮的 source+时刻+sim 压成短串
  V3 无视线历史时记 blindspot:（不是 NULL——空白也是一种视线状态）
  V4 旧库迁移：无该列的库 amend 后有值（建表段自补列）
  V5 盲区可见性：view_state 显示层可见——memory_amend 回执带视线串

跑法: /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase17_view_state.py
"""
import asyncio, importlib.util, os, sqlite3, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
assert SERVER.exists(), f"server.py not found at {SERVER}"

DB = tempfile.mktemp(suffix=".db")
os.environ["MEMORY_MCP_DB"] = DB
os.environ["EMBEDDING_API_KEY"] = ""      # local 路模式

spec = importlib.util.spec_from_file_location("memory_server", str(SERVER))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

async def call(tool, args):
    return await srv.call_tool(tool, args)

async def main():
    print("═══ Phase 17: view_state 视线记录层 ═══")
    srv._init()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")

    # 前置：一条本体记忆（绕过 MCP 写入，直接 INSERT 最小合法行）
    c.execute("""INSERT INTO narratives(id, content, ntype, tags, embedding, created_at)
                 VALUES(1, '本体记忆·R94覆盖事故复盘', 'general', '[]', NULL, '2026-09-20T00:00:00Z')""")
    # attention_log 由 attention_shared.py 负责（server只在heatmap里兜底建表）——夹具自建
    c.execute("""CREATE TABLE IF NOT EXISTS attention_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, narrative_id INTEGER NOT NULL,
        sim REAL, cluster_name TEXT, source TEXT DEFAULT 't1_prefetch',
        created_at TEXT NOT NULL)""")
    c.commit()

    # ── V2 先照亮再改：amend 应把最后视线记进 view_state ──
    print("── V2 照亮后 amend，视线入账 ──")
    c.execute("""INSERT INTO attention_log(narrative_id, sim, cluster_name, source, created_at)
                 VALUES(1, 0.71, 'emb:#12', 'mcp_search', '2026-09-30T10:00:00Z')""")
    c.execute("""INSERT INTO attention_log(narrative_id, sim, cluster_name, source, created_at)
                 VALUES(1, 0.66, 'emb:#12', 't1_prefetch', '2026-09-30T11:00:00Z')""")
    c.commit()
    r = await call("memory_amend", {"narrative_id": 1, "amendment": "视线测试修订", "reason": "V2"})
    r2 = await call("memory_amend", {"narrative_id": 1, "amendment": "视线测试修订二", "reason": "V2b"})
    row = c.execute("SELECT view_state FROM amendments WHERE reason='V2'").fetchone()
    row2 = c.execute("SELECT view_state FROM amendments WHERE reason='V2b'").fetchone()
    check("V2a amend后view_state有值", row and row["view_state"] is not None,
          str(row["view_state"]) if row else "no row")
    # 最后视线 = 11:00 那条（t1_prefetch），不是 10:00 的 mcp_search
    ok2 = row and "t1_prefetch" in (row["view_state"] or "")
    check("V2b 取最后一条视线（t1_prefetch@11:00）", bool(ok2),
          str(row["view_state"]) if row else "-")
    # 第二次 amend 前无新照亮 → 视线仍是同一条（amend 本身不产生照亮）
    ok3 = row2 and "t1_prefetch" in (row2["view_state"] or "")
    check("V2c 无新照亮时视线不变", bool(ok3), str(row2["view_state"]) if row2 else "-")

    # ── V3 盲区：从未被照亮过的条目 amend ──
    print("── V3 盲区 amend 记 blindspot ──")
    c.execute("""INSERT INTO narratives(id, content, ntype, tags, embedding, created_at)
                 VALUES(2, '从未被照亮的记忆', 'general', '[]', NULL, '2026-09-21T00:00:00Z')""")
    c.commit()
    await call("memory_amend", {"narrative_id": 2, "amendment": "盲区修订", "reason": "V3"})
    row3 = c.execute("SELECT view_state FROM amendments WHERE reason='V3'").fetchone()
    check("V3a 从未照亮→记blindspot", row3 and "blindspot" in (row3["view_state"] or ""),
          str(row3["view_state"]) if row3 else "-")

    # ── V1 列存在 + V4 旧库迁移 ──
    print("── V1/V4 列与迁移 ──")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(amendments)").fetchall()]
    check("V1a amendments.view_state列存在", "view_state" in cols, ",".join(cols))

    # V4: 模拟旧库——DB_PATH 是模块级常量，改环境变量不够，直接改模块属性
    db2 = tempfile.mktemp(suffix=".db")
    c2 = sqlite3.connect(db2)
    c2.execute("""CREATE TABLE amendments(id INTEGER PRIMARY KEY AUTOINCREMENT,
        narrative_id INTEGER NOT NULL, amendment TEXT NOT NULL, reason TEXT,
        created_at TEXT NOT NULL)""")
    c2.commit()
    srv.DB_PATH = db2          # 指到旧库
    srv._init()                # 重跑迁移
    cols2 = [r[1] for r in c2.execute("PRAGMA table_info(amendments)").fetchall()]
    check("V4a 旧库(无列)_init自动补view_state", "view_state" in cols2, ",".join(cols2))
    srv.DB_PATH = DB           # 指回测试主库
    c2.close()

    # ── V5 显示层：amend 回执带视线 ──
    print("── V5 回执可见 ──")
    r5 = await call("memory_amend", {"narrative_id": 1, "amendment": "回执测试", "reason": "V5"})
    receipt = r5[0].text if r5 else ""
    check("V5a 回执含视线短串", "视线" in receipt and ("t1_prefetch" in receipt or "blindspot" in receipt),
          receipt[:120])

    c.close()
    print()
    failed = [n for n, ok in results if not ok]
    print(f"{'🎉' if not failed else '💥'} {len(results)-len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

asyncio.run(main())
