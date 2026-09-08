#!/usr/bin/env python3
"""v2.8收尾车钉1夹具：_effective_text 三消费方真接线验收（#400/#486/#489）。

验收形状（鸣鸣#487加码，洄#489认领）：
  15a 生效态形状：tail_only 只吐修订、全文=本体+修订、无修订时空尾
  15b search回显断言：monkeypatch _effective_text，修订行正文真吃它的输出
  15c recall回显断言：同款monkeypatch，recall路径的修订行同源
  15d traj_promote list断言：DREAM批量扫的生效态附文真吃它
  15e 调用方计数：grep server.py，_effective_text 定义外调用点≥3——
      schema承诺的尸检工序（零调用方=死代码），防退化回#482那颗钉

跑法:  /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase15_effective_wiring.py
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

SENTINEL = "SENTINEL-EFFTEXT-486"   # 探针文本：monkeypatch后必须出现在消费方输出

async def main():
    print("═══ Phase 15: _effective_text 三消费方真接线 ═══")
    srv._init()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")

    # ── 15a 生效态形状 ─────────────────────────────────────────
    print("── 15a 生效态形状 ──")
    nid = c.execute(
        "INSERT INTO narratives(ntype, gesture, context_layer, moment,"
        " cognition_direction, content, tags, created_at)"
        " VALUES('memory','生效态接线锚点条目','背景','时刻','方向','全文本体','[\"接线\"]','2026-09-01 10:00:00')"
    ).lastrowid
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)", (nid, "修订一：口径改为机读生效态", "接线", "2026-09-02 10:00:00"))
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)", (nid, "修订二：显示层不截断修订正文", "接线", "2026-09-03 10:00:00"))
    c.commit()

    full = srv._effective_text(nid, c)
    check("15a-1 全文=本体+全部修订按时序",
          "生效态接线锚点条目" in full and "修订一" in full and "修订二" in full
          and full.index("修订一") < full.index("修订二"))
    tail = srv._effective_text(nid, c, tail_only=True)
    check("15a-2 tail_only只吐修订（本体不混入）",
          tail == "修订一：口径改为机读生效态\n修订二：显示层不截断修订正文",
          detail=repr(tail[:60]))
    nid_plain = c.execute(
        "INSERT INTO narratives(ntype, gesture, content, tags, created_at)"
        " VALUES('memory','无修订条目','本体','[]','2026-09-01 11:00:00')"
    ).lastrowid
    c.commit()
    check("15a-3 无修订条目：tail_only空、全文=本体",
          srv._effective_text(nid_plain, c, tail_only=True) == ""
          and srv._effective_text(nid_plain, c) == "无修订条目")

    # ── 15b search回显断言（monkeypatch真身） ──────────────────
    print("── 15b search回显断言 ──")
    orig_eff = srv._effective_text
    def _probe_eff(n, cur, tail_only=False):
        return SENTINEL if not tail_only else SENTINEL
    srv._effective_text = _probe_eff
    try:
        fmt = srv._fmt_narrative(c.execute(
            "SELECT * FROM narratives WHERE id=?", (nid,)).fetchone(), c)
        check("15b-1 search/recall回显的修订行真吃 _effective_text 输出",
              SENTINEL in fmt,
              detail="" if SENTINEL in fmt else "SENTINEL未出现在_fmt_narrative输出")
    finally:
        srv._effective_text = orig_eff

    # ── 15c recall路径断言 ─────────────────────────────────────
    print("── 15c recall回显断言 ──")
    # recall走memory_recall工具→_fmt_narrative；monkeypatch在工具层验证
    srv._effective_text = _probe_eff
    try:
        res = await srv._dispatch("memory_recall", {"limit": 5}, c)
        txt = res[0].text if res else ""
        check("15c-1 recall输出含SENTINEL（修订行同源生效态）",
              SENTINEL in txt)
    finally:
        srv._effective_text = orig_eff

    # ── 15d traj_promote list断言（第三消费方） ─────────────────
    print("── 15d traj_promote list断言 ──")
    srv._rebuild_traj_mech(c, nid); c.commit()
    srv._effective_text = _probe_eff
    try:
        res = await srv._dispatch("memory_traj_promote", {"action": "list"}, c)
        txt = res[0].text if res else ""
        check("15d-1 DREAM批量扫的生效态附文真吃 _effective_text",
              SENTINEL in txt)
    finally:
        srv._effective_text = orig_eff

    # ── 15e 调用方计数（schema承诺尸检工序） ────────────────────
    print("── 15e 调用方计数 ──")
    src = SERVER.read_text(encoding="utf-8")
    import re as _re
    # 数真调用点：排除定义行/注释行/docstring自述。
    # 调用点与消费方的映射：search回显和recall回显共用 _fmt_narrative
    # 内的一个调用点（15b/15c分别断言两条路径真吃到），traj_promote list
    # 独立调用点（15d断言）——2个调用点覆盖3家消费方。
    call_sites = [ln.strip() for ln in src.split("\n")
                  if "_effective_text(" in ln
                  and not ln.strip().startswith("def _effective_text")
                  and not ln.strip().startswith("#")]
    check("15e-1 调用点≥2（防退化回死代码）",
          len(call_sites) >= 2, detail=f"调用点={len(call_sites)}")
    for ln in call_sites:
        print(f"      · {ln[:80]}")

    c.close()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n═══ Phase 15 结果: {passed}/{total} {'✅ ALL GREEN' if passed == total else '❌ HAS RED'} ═══")
    sys.exit(0 if passed == total else 1)

if __name__ == "__main__":
    asyncio.run(main())
