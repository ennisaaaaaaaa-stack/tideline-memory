#!/usr/bin/env python3
"""v2.x salience 读口夹具（HN gate 三件套之一）：显著性过滤器的只读背景检索通道。

背景（spec: ~/drafts/salience-read-port-spec-v0.md）：
  显著性 = 输入 × 当下上下文。过滤器问 Tideline 两类问题——「她在乎什么」
  （存量认知）与「现在什么档」（插槽配置）。本模块只做第一类：只读检索。
  读口返回背景材料，不返回分数；打分器住在过滤器插槽（二号槽）。

验收点：
  V1 三架命中 top1（假数据）：self=自我概念子串命中 / others=画像实体命中 /
     terrain=topic_clusters 名字匹配路由
  V2 emb 路由：假向量最近邻（cosine）+ 无 vec 时跳过 + 零/负相关被滤掉
  V3 social 分层：ptype='user' 与普通实体分层可见，未命中画像者不入列
  V4 pin 注册表：ensure 幂等 + active 快照实时（加一条快照变一条）
  V5 ambient 配额：满/未满两态 + 按日重置（配额制非阈值制 3+1）
  V6 全表缺失优雅降级：不炸、不建表、结构完整
  V7 luck_gate：eps=0 全不过、eps=1 全过（固定 seed）、eps=0.05 可复现
  V8 now_state：最新 snapshot + 活跃 threads（done 不入列）
  V9 契约：docstring 声明「背景材料/二号槽」+ 返回键集合

跑法: /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase19_salience_read.py
"""
import importlib.util, random, sqlite3, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "salience_read.py"
assert MODULE.exists(), f"salience_read.py not found at {MODULE}"

spec = importlib.util.spec_from_file_location("salience_read", str(MODULE))
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

# ── 测试库：表结构逐列抄 server.py / scripts/soft_clusters.py 的真实 DDL ──
DDL = """
CREATE TABLE IF NOT EXISTS self_concept(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(field)
);
CREATE TABLE IF NOT EXISTS profiles(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    ptype TEXT DEFAULT 'contact',
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(entity, ptype)
);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    importance INTEGER,
    emotional INTEGER,
    recurrence INTEGER,
    unresolved INTEGER,
    weight REAL,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL,
    explored_at TEXT
);
CREATE TABLE IF NOT EXISTS topic_clusters(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_name TEXT NOT NULL UNIQUE,
    noun_freq INTEGER DEFAULT 0,
    narrative_ids TEXT DEFAULT '[]',
    last_active TEXT,
    avg_weight REAL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS emb_clusters(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    centroid TEXT NOT NULL,
    member_count INTEGER DEFAULT 0,
    total_hits INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

def seed(conn):
    c = conn
    c.executescript(DDL)
    # self 架假数据（field 有 UNIQUE 约束，一 field 一行）
    c.execute("INSERT INTO self_concept(field,content,updated_at) VALUES('fact','设计师喜欢在深夜听雨声','2026-09-28T01:00:00Z')")
    c.execute("INSERT INTO self_concept(field,content,updated_at) VALUES('self_reflection','我是hui，替设计师守记忆库','2026-09-28T02:00:00Z')")
    c.execute("INSERT INTO self_concept(field,content,updated_at) VALUES('terrain','现象地形：通勤路上的便利店','2026-09-28T03:00:00Z')")
    # others / social 假数据
    c.execute("INSERT INTO profiles(entity,ptype,content,updated_at) VALUES('设计师','user','本机用户，命名权归她','2026-10-01T12:00:00Z')")
    c.execute("INSERT INTO profiles(entity,ptype,content,updated_at) VALUES('设计师','impression','下雨天情绪会变好','2026-10-01T09:00:00Z')")
    c.execute("INSERT INTO profiles(entity,ptype,content,updated_at) VALUES('小明','contact','同事，周会对接口','2026-09-20T08:00:00Z')")
    # terrain 路由A：topic 名字匹配
    c.execute("INSERT INTO topic_clusters(cluster_name,noun_freq,avg_weight) VALUES('雨声白噪音',12,0.8)")
    c.execute("INSERT INTO topic_clusters(cluster_name,noun_freq,avg_weight) VALUES('季度汇报',30,0.9)")
    # terrain 路由B：假 3 维向量（绝不真调 embed 服务——冷启动 19.8s）
    c.execute("INSERT INTO emb_clusters(name,centroid,member_count,created_at,updated_at) VALUES('猫窝','[1.0, 0.0, 0.0]',5,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')")
    c.execute("INSERT INTO emb_clusters(name,centroid,member_count,created_at,updated_at) VALUES('雨林','[0.0, 1.0, 0.0]',8,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')")
    c.execute("INSERT INTO emb_clusters(name,centroid,member_count,created_at,updated_at) VALUES('石滩','[-1.0, 0.0, 0.0]',3,'2026-09-01T00:00:00Z','2026-09-01T00:00:00Z')")
    # now_state 假数据
    c.execute("INSERT INTO snapshots(content,created_at) VALUES('旧的：刚睡下','2026-10-01T22:00:00Z')")
    c.execute("INSERT INTO snapshots(content,created_at) VALUES('刚睡醒，在写代码','2026-10-02T08:30:00Z')")
    c.execute("INSERT INTO threads(content,importance,unresolved,status,created_at) VALUES('HN gate 三件套推进',4,4,'open','2026-09-30T10:00:00Z')")
    c.execute("INSERT INTO threads(content,importance,unresolved,status,created_at) VALUES('已完结的旧线索',2,1,'done','2026-09-01T10:00:00Z')")
    c.commit()

def main():
    print("═══ Phase 19: salience 读口（显著性过滤器背景检索） ═══")
    DB = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(DB)
    seed(conn)

    # ── V1 三架命中 top1 ──
    print("── V1 三架结构共振 ──")
    ev = {"entities": ["设计师", "雨声"], "vec": [1.0, 0.0, 0.0]}
    ctx = sr.salience_context(conn, ev)
    res = ctx["resonance"]
    check("V1a self架 top1 命中（双实体>单实体）",
          res["self"] and res["self"][0]["field"] == "fact" and res["self"][0]["score"] >= 2.0,
          f"top1={res['self'][0] if res['self'] else None}")
    check("V1b self架 条目带 matched 与 score",
          res["self"] and set(res["self"][0]) >= {"field", "content", "matched", "score"})
    check("V1c others架 top1=设计师(user行,updated_at最新)",
          res["others"] and res["others"][0]["entity"] == "设计师" and res["others"][0]["ptype"] == "user",
          f"top1={res['others'][0] if res['others'] else None}")
    topic = [t for t in res["terrain"] if t.get("route") == "topic"]
    check("V1d terrain topic路由 top1 名字含『雨声』且带分",
          topic and "雨声" in topic[0]["name"] and topic[0]["score"] > 0,
          f"topic={topic[:1]}")

    # ── V2 emb 路由 ──
    print("── V2 emb 路由（假向量最近邻） ──")
    emb = [t for t in res["terrain"] if t.get("route") == "emb"]
    check("V2a vec=[1,0,0] 最近邻=猫窝 score≈1.0",
          emb and emb[0]["name"] == "猫窝" and emb[0]["score"] > 0.99,
          f"emb={emb[:2]}")
    check("V2b 零相关(雨林)与负相关(石滩)被滤掉",
          emb and all(t["name"] == "猫窝" for t in emb),
          f"names={[t['name'] for t in emb]}")
    ctx_novec = sr.salience_context(conn, {"entities": ["雨声"]})
    emb_nv = [t for t in ctx_novec["resonance"]["terrain"] if t.get("route") == "emb"]
    check("V2c 事件无 vec → emb 路由跳过（不炸）", emb_nv == [], f"emb={emb_nv}")

    # ── V3 social 分层 ──
    print("── V3 重要他人分层 ──")
    sw = sr.salience_context(conn, {"entities": ["设计师", "小明", "路人"]})["social_weight"]
    check("V3a user 层与 other 层分层可见",
          sw.get("设计师") == "user" and sw.get("小明") == "other", f"sw={sw}")
    check("V3b 未命中画像的实体不入列", "路人" not in sw, f"sw={sw}")

    # ── V4 pin 注册表 ──
    print("── V4 pin 注册表（硬约束，实时读） ──")
    sr.ensure_pin_registry(conn)
    sr.ensure_pin_registry(conn)  # 幂等
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at) VALUES('每天读一首诗',1.0,1,'2026-10-01T00:00:00Z')")
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at) VALUES('盯住HN gate上线',2.0,1,'2026-10-01T00:00:00Z')")
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at) VALUES('过期的pin',1.0,0,'2026-10-01T00:00:00Z')")
    conn.commit()
    pins1 = sr.pins_snapshot(conn)
    check("V4a active 快照=2 条（inactive 不入列）且 weight 实读",
          len(pins1) == 2 and pins1[1]["weight"] == 2.0, f"pins={[p['pin_text'] for p in pins1]}")
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at) VALUES('新加的pin',1.0,1,'2026-10-02T00:00:00Z')")
    conn.commit()
    pins2 = sr.pins_snapshot(conn)
    check("V4b 快照实时（加一条后变三条）", len(pins2) == 3, f"n={len(pins2)}")
    sr.ensure_pin_registry(conn)   # 再 ensure 一遍：幂等不洗数据
    n_after_ensure = conn.execute("SELECT COUNT(*) FROM pin_registry").fetchone()[0]
    check("V4c ensure 幂等不洗数据", n_after_ensure == 4, f"n={n_after_ensure}")

    # ── V4.1 expires_at 过滤（拍板②主行为，zhaozhao探针指出夹具缺口后补钉 2026-10-04）──
    # 日期动态生成（夹具永不腐烂）：now 钉死一个已知时刻，过期=now-1天，未过期=now+30天
    V41_NOW = "2026-10-04T00:00:00Z"
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at,expires_at) "
                 "VALUES('真过期pin（昨天到期）',1.0,1,'2026-09-01T00:00:00Z','2026-10-03T00:00:00Z')")
    conn.execute("INSERT INTO pin_registry(pin_text,weight,active,created_at,expires_at) "
                 "VALUES('未过期pin（还有30天）',1.0,1,'2026-09-01T00:00:00Z','2026-11-03T00:00:00Z')")
    conn.commit()
    pins3 = sr.pins_snapshot(conn, now=V41_NOW)
    names3 = [p["pin_text"] for p in pins3]
    check("V4d 过期滤掉、未过期保留", "真过期pin（昨天到期）" not in names3 and "未过期pin（还有30天）" in names3,
          f"pins={names3}")
    check("V4e 边界严格比较：expires_at == now 按过期处理（「>」非「>=」）",
          all(p["expires_at"] != V41_NOW for p in pins3))
    # 生产路径（now=None → utcnow）：用真实当前时刻反向钉——过期的进不来
    prod_snap = sr.pins_snapshot(conn)  # 生产路径真跑
    check("V4f 生产路径 now=None 真跑：过期 pin 不入快照",
          all(p["pin_text"] != "真过期pin（昨天到期）" for p in prod_snap),
          f"prod_n={len(prod_snap)}")

    # ── V5 ambient 配额 ──
    print("── V5 余光档配额（3+1 保留席） ──")
    sr.ensure_ambient_feed(conn)
    aq_empty = sr.ambient_quota(conn, "2026-10-02")
    check("V5a 未满态 filled=0 quota=3/1",
          aq_empty == {"world": {"filled": 0, "quota": 3}, "china": {"filled": 0, "quota": 1}},
          f"aq={aq_empty}")
    for i in range(3):
        conn.execute("INSERT INTO ambient_feed(region,headline,source,fetched_at) VALUES(?,?,?,?)",
                     ("world", "世界头条%d" % i, "hn", "2026-10-02T07:00:00Z"))
    conn.execute("INSERT INTO ambient_feed(region,headline,source,fetched_at) VALUES('china','国内头条','weibo','2026-10-02T07:00:00Z')")
    conn.execute("INSERT INTO ambient_feed(region,headline,source,fetched_at) VALUES('world','昨天的头条','hn','2026-10-01T07:00:00Z')")
    conn.commit()
    aq_full = sr.ambient_quota(conn, "2026-10-02")
    check("V5b 满态 world filled=3 china filled=1（只算当日）",
          aq_full["world"]["filled"] == 3 and aq_full["china"]["filled"] == 1, f"aq={aq_full}")
    aq_next = sr.ambient_quota(conn, "2026-10-03")
    check("V5c 配额按日重置", aq_next["world"]["filled"] == 0 and aq_next["china"]["filled"] == 0)
    ctx_am = sr.salience_context(conn, {"entities": []})
    check("V5d salience_context 自带 ambient_quota", "ambient_quota" in ctx_am and ctx_am["ambient_quota"]["world"]["quota"] == 3)

    # ── V6 全表缺失优雅降级 ──
    print("── V6 全表缺失降级 ──")
    bare = sqlite3.connect(":memory:")
    try:
        ctx0 = sr.salience_context(bare, {"entities": ["设计师"], "vec": [1.0, 0.0, 0.0]})
        r0 = ctx0["resonance"]
        ok6 = (r0["self"] == [] and r0["others"] == [] and r0["terrain"] == []
               and ctx0["pins"] == [] and ctx0["social_weight"] == {}
               and ctx0["now_state"] == {}
               and ctx0["ambient_quota"]["world"]["quota"] == 3)
        check("V6a 空库不炸：三架空、pin空、social空、now_state空、配额结构在", ok6, f"ctx0={ctx0}")
        tbls = bare.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        check("V6b 读口是纯读者——降级路径不建表", tbls == 0, f"tables={tbls}")
    except Exception as e:
        check("V6a 空库不炸", False, f"raised {type(e).__name__}: {e}")
        check("V6b 读口是纯读者", False)
    bare.close()

    # ── V7 luck_gate ──
    print("── V7 缘分档原语 ──")
    rng0 = random.Random(7)
    check("V7a eps=0 全不过", all(sr.luck_gate(rng0, 0.0) is False for _ in range(50)))
    rng1 = random.Random(7)
    check("V7b eps=1 全过", all(sr.luck_gate(rng1, 1.0) is True for _ in range(50)))
    ra, rb = random.Random(42), random.Random(42)
    seq_a = [sr.luck_gate(ra, 0.05) for _ in range(50)]
    seq_b = [sr.luck_gate(rb, 0.05) for _ in range(50)]
    check("V7c eps=0.05 同 seed 可复现且返回 bool",
          seq_a == seq_b and all(isinstance(x, bool) for x in seq_a),
          f"hits={sum(seq_a)}/50")
    check("V7d 默认 eps 可配置传入", sr.luck_gate(random.Random(1), 0.5) in (True, False))

    # ── V8 now_state ──
    print("── V8 now_state ──")
    ns = sr.salience_context(conn, {"entities": []})["now_state"]
    check("V8a 最新 snapshot（不是旧的）",
          ns.get("snapshot", {}).get("content") == "刚睡醒，在写代码", f"ns.snapshot={ns.get('snapshot')}")
    thr = ns.get("threads", [])
    check("V8b 活跃 threads 只含 open（done 不入列）",
          len(thr) == 1 and "HN gate" in thr[0]["content"], f"threads={thr}")

    # ── V9 契约与性能定位 ──
    print("── V9 契约 ──")
    doc = sr.__doc__ or ""
    check("V9a docstring 声明背景材料定位", "背景材料" in doc and "不打分" in doc)
    check("V9b docstring 声明打分器住二号槽", "二号槽" in doc)
    keys = set(sr.salience_context(conn, {"entities": []}).keys())
    check("V9c 返回键集合恰为五档契约",
          keys == {"resonance", "pins", "social_weight", "ambient_quota", "now_state"}, f"keys={keys}")

    t0 = time.perf_counter()
    for _ in range(200):
        sr.salience_context(conn, ev)
    ms = (time.perf_counter() - t0) * 1000
    check("V9d 200 次全 SQL+本地点积 < 5s（零 LLM/零网络）", ms < 5000, f"{ms:.0f}ms total, {ms/200:.2f}ms/次")

    conn.close()
    print()
    failed = [n for n, ok in results if not ok]
    print(f"{'🎉' if not failed else '💥'} {len(results)-len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

main()
