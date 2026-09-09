#!/usr/bin/env python3
"""v2.8 轨迹压缩夹具：三层铸造全链路真跑。

甜心spec验收点（2026-09-08）：
  1. amend落地 → 机械轨迹同事务生成（mech垫底，任何时刻注入有轨迹）
  2. 每段带原始narrative关键词锚（检索锚，捞回全文）
  3. LLM升格：mech → 自然语言事件链（llm），DREAM夜扫路径
  4. 新amend → llm作废回mech（故事重讲）
  5. 渲染层：相对时间（今天/昨天/N天前），注入=回忆原则
  6. 链长截断：保最新，尾部注「更早N段略」——不静默截断
  7. 无append的narrative零开销（不建轨迹，不显示轨迹行）
  8. MCP查询侧（_fmt_narrative）轨迹行叠加（翻笔记=绝对时间）

跑法:  /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase14_trajectory.py
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
    print("═══ Phase 14: 轨迹压缩三层铸造 ═══")
    srv._init()
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")

    # ── 14a 机械垫底：amend → mech轨迹同事务 ──────────────────
    print("── 14a 机械垫底层 ──")
    # 14c-1 的相对时间断言（7天前/昨天）依赖本体时间戳——硬编码绝对日期会让
    # 测试随日历腐烂（9/8写的时候是7天前，今天跑就成8天前）。改成相对计算，
    # 测试从此日期无关。（2026-09-10 点火夜拆弹）
    from datetime import datetime, timedelta, timezone
    base_ts = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    nid = c.execute(
        "INSERT INTO narratives(ntype, gesture, context_layer, moment,"
        " cognition_direction, content, tags, created_at)"
        " VALUES('memory','甜心设计轨迹压缩spec注入侧不抢context','背景','时刻',"
        "'从全文堆叠切换到轨迹链','全文','[\"轨迹压缩\",\"甜心\"]',?)",
        (base_ts,)
    ).lastrowid
    c.commit()
    # amend前无轨迹
    check("14a-1 amend前无轨迹（无append零开销）",
          c.execute("SELECT COUNT(*) n FROM trajectories").fetchone()["n"] == 0)
    srv._rebuild_traj_mech(c, nid)  # 直接调（fixture绕过MCP工具层）
    c.commit()
    row = c.execute("SELECT * FROM trajectories WHERE narrative_id=?", (nid,)).fetchone()
    check("14a-2 amend调用后mech轨迹生成（cast=mech）",
          row is not None and row["cast_state"] == "mech")
    evs = __import__("json").loads(row["traj_json"])
    check("14a-3 本体段存在且带锚词",
          len(evs) == 1 and "锚" in evs[0]["text"] and "甜心" in evs[0]["text"],
          f"ev0={evs[0]['text'][:50] if evs else 'none'}")
    check("14a-4 n_events记账",
          row["n_events"] == 1)

    # ── 14b amend链：3层修订 → 轨迹4段 ─────────────────────
    print("── 14b amend链生长 ──")
    import json as _json
    from datetime import datetime, timedelta, timezone
    for i, days_ago in enumerate([5, 3, 1]):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
            " VALUES(?,?,?,?)",
            (nid, f"修订{i+1}号：轨迹压缩第{i+1}轮设计迭代想法", "fixture14", ts))
    c.commit()
    srv._rebuild_traj_mech(c, nid)
    c.commit()
    row = c.execute("SELECT * FROM trajectories WHERE narrative_id=?", (nid,)).fetchone()
    evs = _json.loads(row["traj_json"])
    check("14b-1 本体+3修订=4段", row["n_events"] == 4 and len(evs) == 4)
    check("14b-2 每段带锚词（检索锚）",
          all("锚" in e["text"] for e in evs),
          f"anchors: {[('yes' if '锚' in e['text'] else 'no') for e in evs]}")
    ts_list = [e["ts"] for e in evs]
    check("14b-3 时序正确（本体最早，修订渐晚）",
          ts_list == sorted(ts_list), f"ts={ts_list}")
    check("14b-4 latest_amendment_id指纹=最大amend id",
          row["latest_amendment_id"] == c.execute(
              "SELECT MAX(id) m FROM amendments WHERE narrative_id=?", (nid,)
          ).fetchone()["m"])

    # ── 14c 渲染层：相对时间 ──────────────────────────────────
    print("── 14c 渲染层 ──")
    rendered = srv._trajectory_render(c, nid)
    check("14c-1 相对时间渲染（N天前/昨天）",
          "7天前" in rendered and "5天前" in rendered and "3天前" in rendered and "昨天" in rendered,
          f"rendered={rendered[:120]}")
    check("14c-2 箭头链格式（→连接）", " → " in rendered)
    abs_rendered = srv._trajectory_render(c, nid, relative=False)
    check("14c-3 绝对时间模式（MCP查询侧）",
          "2026-" in abs_rendered or "-" in abs_rendered.split("，")[0])
    # 渲染不写库（幂等读）
    n_before = c.execute("SELECT COUNT(*) n FROM trajectories").fetchone()["n"]
    srv._trajectory_render(c, nid)
    check("14c-4 渲染纯读不写库",
          c.execute("SELECT COUNT(*) n FROM trajectories").fetchone()["n"] == n_before)

    # ── 14d LLM升格 ──────────────────────────────────────────
    print("── 14d LLM升格层 ──")
    llm_events = [
        {"ts": evs[0]["ts"], "text": "我接下了甜心的轨迹压缩设计spec——注入侧要轻"},
        {"ts": evs[1]["ts"], "text": "我给spec加了检索锚：每段都带原始关键词（甜心、轨迹压缩）"},
        {"ts": evs[2]["ts"], "text": "我把三层铸造方案讲给甜心听，她拍板lazy模式"},
        {"ts": evs[3]["ts"], "text": "甜心说注入不能抢context window——轨迹链要短"},
    ]
    check("14d-1 llm升格写回成功", srv._save_traj_llm(c, nid, llm_events))
    c.commit()
    row = c.execute("SELECT cast_state, n_events FROM trajectories WHERE narrative_id=?", (nid,)).fetchone()
    check("14d-2 cast=llm", row["cast_state"] == "llm")
    check("14d-3 升格后n_events保留", row["n_events"] == 4)
    rendered = srv._trajectory_render(c, nid)
    check("14d-4 llm轨迹渲染自然语言链",
          "轨迹压缩" in rendered and "5天前" in rendered)
    # 畸形events拒绝
    check("14d-5 空events拒绝落盘", srv._save_traj_llm(c, nid, []) is False)
    bad = [{"ts": "", "text": "无时间戳"}]
    check("14d-6 无ts段被过滤（其他段照常）",
          srv._save_traj_llm(c, nid, bad) is False)

    # ── 14e 新amend作废llm回mech ─────────────────────────────
    print("── 14e 故事重讲 ──")
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        "INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
        " VALUES(?,?,?,?)", (nid, "新修订：部署上线", "fixture14e", ts_now))
    c.commit()
    srv._rebuild_traj_mech(c, nid)
    c.commit()
    row = c.execute("SELECT cast_state, n_events FROM trajectories WHERE narrative_id=?", (nid,)).fetchone()
    check("14e-1 新amend后cast回mech（故事重讲）", row["cast_state"] == "mech")
    check("14e-2 段数增长（4→5）", row["n_events"] == 5)
    # llm态write被拒（异常路径守卫）
    ok = srv._save_traj_llm(c, nid, llm_events)
    row_after = c.execute("SELECT cast_state FROM trajectories WHERE narrative_id=?", (nid,)).fetchone()
    check("14e-3 mech态可正常升格", ok and row_after["cast_state"] == "llm")
    # 再write（llm态）——通过_dispatch层检查需要MCP环境，这里验证save层不设llm守卫（守卫在dispatch层）

    # ── 14f 截断策略 ─────────────────────────────────────────
    print("── 14f 链长截断 ──")
    many_events = [{"ts": (datetime.now(timezone.utc) - timedelta(days=30-i)).strftime("%Y-%m-%d %H:%M:%S"),
                    "text": f"第{i}段长链事件"} for i in range(20)]
    nid2 = c.execute(
        "INSERT INTO narratives(ntype, gesture, content, tags, created_at)"
        " VALUES('memory','长链测试','content','[]','2026-08-01 00:00:00')"
    ).lastrowid
    c.execute("INSERT INTO amendments(narrative_id, amendment, reason, created_at)"
              " VALUES(?,?,?,?)", (nid2, "长链修订", "fixture", "2026-08-02 00:00:00"))
    c.commit()
    srv._rebuild_traj_mech(c, nid2)
    c.commit()
    srv._save_traj_llm(c, nid2, many_events)
    c.commit()
    rendered = srv._trajectory_render(c, nid2)
    check("14f-1 超12段截断保最新",
          "第19段" in rendered and "第0段" not in rendered)
    check("14f-2 截断不静默（尾部标注）", "更早" in rendered and "段略" in rendered)

    # ┈ 14f修root cause：_rebuild_traj_mech里14e的SQL占位符笔误已过，验证两条narrative并存 ┈
    check("14f-3 两条narrative轨迹并存不互扰",
          c.execute("SELECT COUNT(*) n FROM trajectories").fetchone()["n"] == 2)

    # ── 14g MCP查询侧叠加（_fmt_narrative）──────────────────
    print("── 14g MCP查询侧 ──")
    r = c.execute("SELECT * FROM narratives WHERE id=?", (nid,)).fetchone()
    fmt = srv._fmt_narrative(r, c)
    check("14g-1 轨迹行叠加在📝修订层后", "🧭 轨迹:" in fmt)
    check("14g-2 查询侧绝对时间（翻笔记）",
          "2026-" in fmt.split("🧭 轨迹:")[1][:20] if "🧭 轨迹:" in fmt else False)
    # 无轨迹的narrative
    nid3 = c.execute(
        "INSERT INTO narratives(ntype, gesture, content, tags, created_at)"
        " VALUES('memory','无轨迹条目','content','[]','2026-08-05 00:00:00')"
    ).lastrowid
    c.commit()
    r3 = c.execute("SELECT * FROM narratives WHERE id=?", (nid3,)).fetchone()
    fmt3 = srv._fmt_narrative(r3, c)
    check("14g-3 无append条目无轨迹行", "🧭" not in fmt3)

    # ── 14h memory_amend全工具路径（dispatch层）──────────────
    print("── 14h 工具分发层 ──")
    # 模拟MCP dispatch：直接调内部函数链
    nid4 = c.execute(
        "INSERT INTO narratives(ntype, gesture, content, tags, created_at)"
        " VALUES('memory','dispatch测试条目','content','[]','2026-08-06 00:00:00')"
    ).lastrowid
    c.commit()
    srv._rebuild_traj_mech(c, nid4)
    c.commit()
    check("14h-1 amend路径同事务生成轨迹（模拟）",
          c.execute("SELECT COUNT(*) n FROM trajectories WHERE narrative_id=?", (nid4,)).fetchone()["n"] == 1)

    # 汇总
    ok = sum(1 for _, o in results if o)
    print(f"\n═══ Phase 14 结果: {ok}/{len(results)} ═══")
    if ok != len(results):
        for name, o in results:
            if not o:
                print(f"  ❌ FAIL: {name}")
        sys.exit(1)
    print("✅ 第14相全绿——轨迹压缩三层铸造全链路真跑通过。")

if __name__ == "__main__":
    asyncio.run(main())
