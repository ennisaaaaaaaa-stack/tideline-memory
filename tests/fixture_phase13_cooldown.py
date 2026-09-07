#!/usr/bin/env python3
"""v2.7.1 夹具第13相：分层冷却四根钉 + 批量路冷却生效。

13c 拍板形状（鸣鸣 01:59 四根钉 + 洄补第5根）：
  1. 连接类失败（断网）→ 全局窗开，一次付清；首连败即收手（比「连败3条
     早退」更强的形状——挂起型断网只烧 1×(1×10s) 不是 N 次）
  2. 条目类失败（服务活、单条被拒）→ 窗不开、记 per-nid 一条一账，
     别的 nid 照铸
  3. 有成功即清窗
  4. 夜扫 force=True 翻窗；批量路（nids=None）受同一张冷却表管辖
  5. 补铸预算砍短：1 次重试 × 10s（副业不继承主业 3×60s）

跑法:  /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase13_cooldown.py
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

# ── embed 注入：行为可编程，全程零网络 ─────────────────────
CALLS: list = []

def make_embed(behavior):
    """behavior(text) -> ('vec', [f]) | ('none', None) | ('probe_ok', ...)"""
    async def fake_embed(text, _retries=3, timeout=60.0, **kw):
        CALLS.append({"text": text, "_retries": _retries, "timeout": timeout})
        kind, payload = behavior(text)
        if kind == "vec":
            return [0.1, 0.2, 0.3]
        return None
    return fake_embed

PROBE = srv._AMVEC_PROBE_TEXT
results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

def seed(c, n=3):
    nid = c.execute(
        "INSERT INTO narratives(ntype, gesture, context_layer, moment,"
        " cognition_direction, content, tags, created_at)"
        " VALUES('memory','g','c','m','d','夹具13相叙事','[]','2026-09-08 00:00:00')"
    ).lastrowid
    aids = []
    for i in range(n):
        aid = c.execute(
            "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
            " VALUES(?,?,?,?)",
            (nid, f"修订文本{i}号", "fixture13", "2026-09-08 00:00:30"),
        ).lastrowid
        aids.append(aid)
    c.commit()
    return nid, aids

async def main():
    print("═══ Phase 13: 分层冷却（13c 四根钉+1） ═══")
    srv._init()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row

    # ── 13a 连接类：全局窗一次付清，首连败即收手 ─────────────
    print("── 13a 连接类断网：全局窗 + 首连败收手 ──")
    nid, aids = seed(c, 3)
    srv._embed = make_embed(lambda t: ("none", None))  # 全死（含哨兵）
    CALLS.clear()
    await srv._ensure_amvec_sync(c, None)
    c.commit()
    n_vec = c.execute("SELECT COUNT(*) n FROM amendment_vectors").fetchone()["n"]
    check("13a-1 断网期修订不被吞（amend 仍全部在库）",
          c.execute("SELECT COUNT(*) n FROM amendments").fetchone()["n"] == 3)
    check("13a-2 零向量落库（服务死→什么都不铸）", n_vec == 0)
    check("13a-3 全局窗已开（scope='global' 一行）",
          c.execute("SELECT COUNT(*) n FROM amvec_cooldown"
                    " WHERE scope='global'").fetchone()["n"] == 1)
    # 首条 cast 失败 + 1 次哨兵 = 2 次调用后收手（不重烧第2/3条）
    check("13a-4 首连败即收手（embed 只烧 2 次：1条+1哨兵，非 N×2）",
          len(CALLS) == 2, f"calls={len(CALLS)}")
    check("13a-5 挂起型单次预算=1重试×10s（非3×60s）",
          CALLS and CALLS[0]["_retries"] == 1 and CALLS[0]["timeout"] == 10.0,
          f"retries={CALLS[0]['_retries']}, timeout={CALLS[0]['timeout']}" if CALLS else "no calls")
    # 窗内：查询路补铸直接跳过（零调用）
    CALLS.clear()
    await srv._ensure_amvec_sync(c, [nid])
    check("13a-6 全局窗内查询路跳过补铸（0 次网络）", len(CALLS) == 0)
    # 窗过期后恢复：试铸成功即清窗
    srv._embed = make_embed(lambda t: ("vec", None))
    expired = c.execute(
        "UPDATE amvec_cooldown SET failed_at=datetime('now','-16 minutes')"
        " WHERE scope='global'").rowcount
    await srv._ensure_amvec_sync(c, None)
    c.commit()
    check("13a-7 窗过期+服务恢复 → 全量补铸成功",
          c.execute("SELECT COUNT(*) n FROM amendment_vectors").fetchone()["n"] == 3)
    check("13a-8 有成功即清窗（cooldown 表清空）",
          c.execute("SELECT COUNT(*) n FROM amvec_cooldown").fetchone()["n"] == 0)

    # ── 13b 条目类：一条一账，不株连 ─────────────────────────
    print("── 13b 条目类被拒：per-nid 一条一账，不株连全表 ──")
    nid2, aids2 = seed(c, 3)  # 再来 3 条新修订
    BAD_TEXT = "坏文本"
    c.execute("UPDATE amendments SET amendment=? WHERE id=?",
              (BAD_TEXT, aids2[0])); c.commit()
    def behavior(t):
        if t == BAD_TEXT:
            return ("none", None)   # 这条被拒
        return ("vec", None)         # 其他都成（哨兵也成）
    srv._embed = make_embed(behavior)
    CALLS.clear()
    await srv._ensure_amvec_sync(c, [nid2])
    c.commit()
    vecs2 = {r["amendment_id"] for r in c.execute(
        "SELECT amendment_id FROM amendment_vectors")}
    check("13b-1 坏条目不铸（无向量），好条目照铸（2/3）",
          aids2[0] not in vecs2 and aids2[1] in vecs2 and aids2[2] in vecs2)
    check("13b-2 全局窗不开（服务活着）",
          c.execute("SELECT COUNT(*) n FROM amvec_cooldown"
                    " WHERE scope='global'").fetchone()["n"] == 0)
    check("13b-3 坏条目记 per-nid 账（amend:<id> 一行）",
          c.execute("SELECT COUNT(*) n FROM amvec_cooldown"
                    " WHERE scope=?", (f"amend:{aids2[0]}",)).fetchone()["n"] == 1)
    # 窗内重试同条：跳过（不再烧）；别的 nid 照常
    CALLS.clear()
    await srv._ensure_amvec_sync(c, [nid2])
    check("13b-4 条目窗内同条跳过（只试好条目漏铸部分=0 次新网络）",
          len(CALLS) == 0)
    # force 翻窗：夜扫重试坏条目（服务可能已恢复）
    CALLS.clear()
    await srv._ensure_amvec_sync(c, None, force=True)
    c.commit()
    check("13b-5 夜扫 force 翻条目窗，坏条目重试（≥1 次调用）",
          len(CALLS) >= 1)

    # ── 13c 批量路同表管辖（nids=None 不绕冷却）──────────────
    print("── 13c 批量路受同一张冷却表管辖 ──")
    # 制造 3 条漏铸 + 全局窗开着
    c.execute("DELETE FROM amendment_vectors"); c.commit()
    c.execute("INSERT OR REPLACE INTO amvec_cooldown(scope, model_ns, failed_at)"
              " VALUES('global', ?, datetime('now'))",
              (srv._EMB_MODEL + "|local",)); c.commit()
    srv._embed = make_embed(lambda t: ("vec", None))
    CALLS.clear()
    await srv._ensure_amvec_sync(c, None)   # 非 force
    c.commit()
    check("13c-1 全局窗内批量路整体跳过（0 次网络）", len(CALLS) == 0)
    check("13c-2 窗内批量路不落任何向量",
          c.execute("SELECT COUNT(*) n FROM amendment_vectors").fetchone()["n"] == 0)
    await srv._ensure_amvec_sync(c, None, force=True)
    c.commit()
    check("13c-3 夜扫 force 翻全局窗，批量补齐 6 条修订",
          c.execute("SELECT COUNT(*) n FROM amendment_vectors").fetchone()["n"] == 6)
    check("13c-4 force 补齐后清窗",
          c.execute("SELECT COUNT(*) n FROM amvec_cooldown").fetchone()["n"] == 0)

    # ── 汇总 ─────────────────────────────────────────────────
    ok = sum(1 for _, o in results if o)
    print(f"\n═══ Phase 13 结果: {ok}/{len(results)} ═══")
    if ok != len(results):
        for name, o in results:
            if not o:
                print(f"  ❌ FAIL: {name}")
        sys.exit(1)
    print("✅ 第13相全绿——13c 分层冷却四根钉+1 全部真跑通过。")

if __name__ == "__main__":
    asyncio.run(main())
