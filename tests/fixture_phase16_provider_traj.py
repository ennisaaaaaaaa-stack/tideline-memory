#!/usr/bin/env python3
"""v2.8 provider 注入渲染层夹具（r64）：轨迹链进注入的真跑验收。

验收点（甜心spec 9/8 + 五问落锁 9/9凌晨）：
  P1 轨迹渲染：mech轨迹 → 「n天前，事件 → n天前，事件」相对时间链
  P2 锚脚手架拆除：注入文本不含「（锚：…）」
  P3 轨迹住母记忆块内：🧭行紧贴母条目gesture行，不建全局小节
  P4 无表防御：trajectories表不存在（v2.8部署前过渡期）→ 静默跳过注入不炸
  P5 无append条目零开销：无轨迹行，渲染与r63等价
  P6 注入侧帽：链长>6段保最新6段，尾部注「更早N段略」——不静默截断
  P7 llm态轨迹：升格后自然语言事件链原样渲染（无锚可拆）

跑法: python3 /tmp/test_provider_traj.py （直接python3，不依赖venv——
      provider插件用stdlib only，MCP venv只在MCP侧夹具用）
"""
import importlib.util, os, sqlite3, sys, tempfile
from pathlib import Path

PLUGIN = Path(os.environ.get("TIDELINE_PROVIDER_PATH",
    "/home/ubuntu/.hermes/plugins/portalk/__init__.py"))
if not PLUGIN.exists():
    print("SKIP: provider plugin not on this machine (production-only fixture)")
    sys.exit(0)

DB = tempfile.mktemp(suffix=".db")
os.environ["MEMORY_MCP_DB"] = DB

# 动态加载provider插件（绕过agent.memory_provider基类依赖——夹具桩替）
import types as _types
stub = _types.ModuleType("agent")
stub_mem = _types.ModuleType("agent.memory_provider")
class MemoryProvider:  # 基类桩
    pass
stub_mem.MemoryProvider = MemoryProvider
sys.modules["agent"] = stub
sys.modules["agent.memory_provider"] = stub_mem

spec = importlib.util.spec_from_file_location("portalk_prov", str(PLUGIN))
prov = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prov)

results = []
def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

def make_db(with_traj_table=True):
    if os.path.exists(DB):
        os.unlink(DB)
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    # narratives最小schema（provider的prefetch只读这些列）
    c.execute("""CREATE TABLE narratives(
        id INTEGER PRIMARY KEY, ntype TEXT, gesture TEXT, content TEXT,
        context_layer TEXT, cognition_direction TEXT, tags TEXT,
        weight REAL, embedding TEXT, created_at TEXT)""")
    c.execute("""CREATE TABLE attention_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT, narrative_id INTEGER,
        sim REAL, source TEXT, created_at TEXT)""")
    if with_traj_table:
        c.execute("""CREATE TABLE trajectories(
            narrative_id INTEGER PRIMARY KEY, traj_json TEXT, n_events INTEGER,
            latest_amendment_id INTEGER, cast_state TEXT,
            created_at TEXT, updated_at TEXT)""")
    c.commit()
    return c

def add_narr(c, gesture, created="2026-09-01 10:00:00", tags='[]'):
    return c.execute(
        "INSERT INTO narratives(ntype,gesture,content,tags,created_at) VALUES(?,?,?,?,?)",
        ("memory", gesture, gesture, tags, created)).lastrowid

def add_traj(c, nid, events, total=None):
    import json
    c.execute(
        "INSERT INTO trajectories(narrative_id,traj_json,n_events,cast_state,created_at,updated_at)"
        " VALUES(?,?,?,?,datetime('now'),datetime('now'))",
        (nid, json.dumps(events, ensure_ascii=False),
         total if total is not None else len(events), "mech"))
    c.commit()

import json

# ── P1+P2+P3: 渲染形状 ──────────────────────────────────────────
print("── P1-P3 渲染形状 ──")
c = make_db()
nid = add_narr(c, "甜心设计轨迹压缩spec", tags='["轨迹压缩","甜心"]')
add_traj(c, nid, [
    {"ts": "2026-09-01 10:00:00", "text": "甜心设计轨迹压缩spec注入侧不抢context（锚：甜心、设计、轨迹压缩）"},
    {"ts": "2026-09-08 10:00:00", "text": "存储侧三层铸造落地（锚：甜心、设计、轨迹压缩）"},
])
rendered = prov._traj_render_inj(c, nid)
check("P1 相对时间链：n天前，事件 → n天前，事件",
      "，" in rendered and " → " in rendered and "天前" in rendered,
      detail=rendered[:80])
check("P2 锚脚手架拆除：注入文本零「（锚：」",
      "（锚：" not in rendered)
check("P2b 拆锚后段文本仍完整（脚手架在尾部，拆掉不伤正文）",
      "甜心设计轨迹压缩spec注入侧不抢context" in rendered
      and "存储侧三层铸造落地" in rendered)

# P3: 轨迹住母块内（整链嵌入同一f-string）
check("P3 轨迹住母记忆块内（渲染函数返回单块文本，无独立小节头）",
      "##" not in rendered and "\n  🧭 " not in rendered)

# ── P4: 无表防御 ────────────────────────────────────────────────
print("── P4 无表防御 ──")
c2 = make_db(with_traj_table=False)
nid2 = add_narr(c2, "老库过渡期条目")
check("P4 trajectories表不存在 → 静默跳过返回空",
      prov._traj_render_inj(c2, nid2) == "")

# ── P5: 无append零开销 ──────────────────────────────────────────
print("── P5 无append零开销 ──")
c3 = make_db()
nid3 = add_narr(c3, "无轨迹条目A")
nid4 = add_narr(c3, "带轨迹条目B")
add_traj(c3, nid4, [{"ts": "2026-09-01 10:00:00", "text": "唯一事件"}])
check("P5 无轨迹条目渲染为空（零开销）",
      prov._traj_render_inj(c3, nid3) == ""
      and prov._traj_render_inj(c3, nid4) != "")

# ── P6: 注入侧帽 ────────────────────────────────────────────────
print("── P6 注入侧帽 ──")
c4 = make_db()
nid5 = add_narr(c4, "长链条目")
evs = [{"ts": f"2026-09-{i:02d} 10:00:00", "text": f"第{i}件事"} for i in range(1, 13)]
add_traj(c4, nid5, evs, total=12)
r6 = prov._traj_render_inj(c4, nid5)
seg_count = r6.count("，")
check("P6 12段链注入侧保最新6段",
      "第7件事" in r6 and "第6件事" not in r6 and seg_count == 6,
      detail=f"段数={seg_count}")
check("P6b 尾部注「更早N段略」——不静默截断",
      "（更早6段略）" in r6)

# ── P7: llm态轨迹 ───────────────────────────────────────────────
print("── P7 llm态轨迹 ──")
c5 = make_db()
nid6 = add_narr(c5, "llm升格条目")
add_traj(c5, nid6, [
    {"ts": "2026-09-01 10:00:00", "text": "那天她说注入要轻，轨迹压缩这个想法第一次落地"},
    {"ts": "2026-09-08 22:00:00", "text": "存储侧三层铸造收绿，她加完班回来看了轨迹渲染的形状"},
])
r7 = prov._traj_render_inj(c5, nid6)
check("P7 llm自然语言链原样渲染（无锚可拆、相对时间）",
      "那天她说注入要轻" in r7 and "天前" in r7 and "，" in r7)

# ── P8: prefetch端到端（渲染点接线验证） ────────────────────────
print("── P8 prefetch端到端 ──")
# 桩掉embedding：让query向量=narrative向量 → sim=1.0 必命中
def _embed_stub(text):
    return [1.0, 0.0, 0.0]
prov._embed = _embed_stub
# narratives.embedding存同款向量
c6 = make_db()
nidA = add_narr(c6, "轨迹压缩注入验收条目")
c6.execute("UPDATE narratives SET embedding=? WHERE id=?",
           (json.dumps([1.0, 0.0, 0.0]), nidA))
add_traj(c6, nidA, [
    {"ts": "2026-09-01 10:00:00", "text": "spec定稿（锚：轨迹压缩）"},
    {"ts": "2026-09-08 22:00:00", "text": "存储侧落地（锚：轨迹压缩）"},
])
c6.commit()
p = prov.TidelineMemoryProvider()
p._injected_ids = set()
out = p.prefetch("轨迹压缩注入验收", session_id="fixture")
check("P8a prefetch输出含🧭轨迹链",
      "🧭" in out and "天前，" in out, detail=(out[:150] if out else "EMPTY"))
check("P8b 轨迹紧贴母条目行（同块，gesture行在前）",
      out.index("轨迹压缩注入验收条目") < out.index("🧭") if "🧭" in out else False)
check("P8c prefetch输出零锚脚手架",
      "（锚：" not in out)

for cc in (c, c2, c3, c4, c5, c6):
    try: cc.close()
    except Exception: pass

passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\n═══ Provider r64 结果: {passed}/{total} {'✅ ALL GREEN' if passed == total else '❌ HAS RED'} ═══")
sys.exit(0 if passed == total else 1)
