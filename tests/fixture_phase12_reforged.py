#!/usr/bin/env python3
"""phase12 重铸夹具（v2.8收尾车钉2, 2026-09-09）：短查询兜底 + 对账探针。

背景：#422/#431 会审车的原版夹具（18→24项）随施工session沙箱湮灭，
repo 里只剩13/14相。照照清单第10条（短查询FTS miss兜底、COUNT对账
探针两个活虫修复的复跑）没有夹具闭不了——#350 那把尺子（干净clone里
真跑）量自己。重铸≠复原：只铸覆盖那两个修复行为的钉，挂「重铸」标记
不冒充原版（鸣鸣#487：别让注释假装架构在；同理不假装历史在）。

验收点：
  12a 短查询兜底：amendment FTS（trigram）够不着<3字查询 → LIKE 兜底命中
  12b 对账探针：external-content FTS 静默漂移 → integrity-check 咬出 → rebuild 自愈
  12c rebuild 后检索路恢复：FTS 主路重新命中
  12d 触发器链不打破：直插 amendments 表（模拟外部写入）后索引应同步

跑法:  /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase12_reforged.py
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

async def main():
    print("═══ Phase 12 (reforged): 短查询兜底 + 对账探针 ═══")
    srv._init()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")

    # 造一条带修订的narrative
    nid = c.execute(
        "INSERT INTO narratives(ntype, gesture, context_layer, moment,"
        " cognition_direction, content, tags, created_at)"
        " VALUES('memory','对账探针母条目','背景','时刻','方向','全文本体','[\"对账\"]','2026-09-01 10:00:00')"
    ).lastrowid
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)",
        (nid, "修订本体：短查询兜底验收锚点文字", "重铸", "2026-09-02 10:00:00"))
    c.commit()

    # ── 12a 短查询兜底 ──────────────────────────────────────────
    print("── 12a 短查询兜底 ──")
    # trigram 对 <3 字查询：FTS MATCH 双字查询应 miss（或异常）→ LIKE 兜底
    res = await srv._dispatch("memory_search", {"query": "兜底"}, c)
    txt = res[0].text if res else ""
    check("12a-1 两字查询（FTS够不着）仍能经LIKE兜底命中修订",
          "短查询兜底" in txt and "对账探针母条目" in txt,
          detail=txt[:100])

    # ── 12b 对账探针：漂移 → 咬出 → 自愈 ────────────────────────
    print("── 12b 对账探针 ──")
    # 模拟外部写入漂移：触发器不在场时直插 amendments（迁移脚本场景），
    # 索引少行——正常INSERT永远走触发器，必须先DROP触发器才造得出漂移
    c.execute("DROP TRIGGER am_fts_ai")
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)",
        (nid, "漂移注入：这条不进FTS索引", "外部写入模拟", "2026-09-03 10:00:00"))
    c.commit()
    c.execute("""CREATE TRIGGER IF NOT EXISTS am_fts_ai AFTER INSERT ON amendments BEGIN
        INSERT INTO amendments_fts(rowid, amendment, reason)
        VALUES (new.id, new.amendment, new.reason);
        END""")
    c.commit()
    n_idx = c.execute(
        "SELECT COUNT(*) n FROM amendments_fts WHERE amendments_fts MATCH '漂移注入'"
    ).fetchone()["n"]
    n_tbl = c.execute(
        "SELECT COUNT(*) n FROM amendments WHERE amendment LIKE '%漂移注入%'"
    ).fetchone()["n"]
    # 注意外部内容表陷阱（server.py :542自己的尸检注释）：COUNT(*)穿透读
    # 内容表永远相等——漂移检测必须用MATCH查询，不能用计数。
    check("12b-1 外部写入后索引漂移真实发生（MATCH查不到新行）",
          n_idx == 0 and n_tbl == 1, detail=f"fts_match={n_idx} table={n_tbl}")

    srv._ensure_amfts_sync(c)   # 对账探针：integrity-check → rebuild
    n_idx2 = c.execute(
        "SELECT COUNT(*) n FROM amendments_fts WHERE amendments_fts MATCH '漂移注入'"
    ).fetchone()["n"]
    check("12b-2 integrity-check咬出漂移 → rebuild自愈（MATCH命中）",
          n_idx2 == 1, detail=f"fts_match={n_idx2}")

    # ── 12c rebuild后主路恢复 ────────────────────────────────────
    print("── 12c 主路恢复 ──")
    res2 = await srv._dispatch("memory_search", {"query": "漂移注入"}, c)
    txt2 = res2[0].text if res2 else ""
    check("12c-1 rebuild后FTS主路命中新修订（不靠LIKE兜底）",
          "漂移注入" in txt2, detail=txt2[:100])

    # ── 12d 触发器链完好 ────────────────────────────────────────
    print("── 12d 触发器链 ──")
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)",
        (nid, "触发器链验收：正常INSERT应自动进索引", "重铸", "2026-09-04 10:00:00"))
    c.commit()
    hit = c.execute(
        "SELECT COUNT(*) n FROM amendments_fts WHERE amendments_fts MATCH '触发器链'"
    ).fetchone()["n"]
    check("12d-1 正常INSERT经触发器自动进FTS索引",
          hit >= 1, detail=f"hits={hit}")

    c.close()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n═══ Phase 12 (reforged) 结果: {passed}/{total} {'✅ ALL GREEN' if passed == total else '❌ HAS RED'} ═══")
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    asyncio.run(main())
