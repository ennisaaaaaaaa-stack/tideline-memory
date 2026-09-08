#!/usr/bin/env python3
"""
Tideline Memory MCP Server

MCP-A (memory_*): Structured memory — narratives, profiles, snapshots
MCP-B (context_*): Full context timeline + semantic search

Environment variables:
  MEMORY_MCP_DB       SQLite path (default: ~/memory/mcp_memory.db)
  EMBEDDING_API_KEY   Embedding API key (optional — enables semantic search)
  EMBEDDING_API_URL   Embedding endpoint (default: Zhipu)
  EMBEDDING_MODEL     Model name (default: embedding-3)
  AGENT_NAME          Agent label for logs (default: "agent")
"""

import os, sys, json, math, sqlite3, hashlib, re
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

# ─── Config ──────────────────────────────────────────────
DB_PATH   = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))
AGENT     = os.environ.get("AGENT_NAME", "agent")

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# ─── v2.5 anti-blanking guards & overwrite history (2026-09-03) ──────────
# 起因：9/2 事故——schema 接受空字符串为合法 content，一次误写清空了
# profiles/self_concept 全部字段，session 上下文成了唯一恢复源。
# 两层修复：①空 content 在门口拒绝（守卫层）②覆盖前旧值归档进
# <table>_history（回滚层）。线上表仍只存最新版——它每 session 注入，
# 历史归档只按需查询。

def _reject_empty(content, what):
    """拒绝会把现有内容清空的空写入。空 content 是 schema 合法值，
    但对覆盖型写入（profiles/self_concept/threads 更新）等于毁数据。"""
    if content is None or not str(content).strip():
        raise ValueError(f"{what}: 拒绝空 content——这会清空现有内容（9/2 事故后加的守卫）")
    return str(content)

def _archive_if_overwritten(c, table, key_cols, key_vals):
    """UPSERT 覆盖前，把旧行整份存进 {table}_history。
    表名/列名是调用点硬编码字面量，不是用户输入——无注入面。"""
    try:
        where = " AND ".join(f"{k}=?" for k in key_cols)
        row = c.execute(f"SELECT content FROM {table} WHERE {where}", key_vals).fetchone()
        if row and str(row["content"] or "").strip():
            c.execute(
                f"INSERT INTO {table}_history({', '.join(key_cols)}, old_content, archived_at)"
                " VALUES(" + ",".join(["?"] * (len(key_cols) + 2)) + ")",
                (*key_vals, row["content"], _now()),
            )
    except sqlite3.OperationalError:
        pass  # 历史表还没迁移——绝不能因为归档失败挡住线上写入

# ─── Database ────────────────────────────────────────────
def _db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def _init():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = _db()
    # Phase 1: Create tables (no triggers/FTS yet — triggers reference
    # v2.3 columns that may not exist on a fresh-from-v2.2 DB)
    c.executescript("""
    -- ═══════ narratives: structured memory (v2.3) ═══════
    CREATE TABLE IF NOT EXISTS narratives(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,           -- legacy free-text (kept for backward compat)
        ntype TEXT DEFAULT 'general',
        tags TEXT DEFAULT '[]',
        embedding TEXT,
        created_at TEXT NOT NULL,
        -- v2.3 structured fields
        gesture TEXT,                    -- 动作/事件，一句话，带语气
        context_layer TEXT,              -- 背景脉络  (avoid clash with SQL keyword)
        moment TEXT,                     -- 日期/时间标记
        cognition_direction TEXT,        -- 认知方向——"从X切换到Y"
        related_entities TEXT,           -- JSON array of entity names
        source_links TEXT,               -- JSON array of context row IDs
        entities_role TEXT,              -- role assignment for multi-entity narratives
        -- v2.3 weight
        weight REAL,                     -- computed weight (0.0-1.0)
        importance INTEGER,              -- 1-5, LLM fills
        emotional INTEGER,               -- 1-5
        recurrence INTEGER,              -- 1-5
        unresolved INTEGER               -- 1-5
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
    CREATE TABLE IF NOT EXISTS context(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        embedding TEXT,
        meta TEXT DEFAULT '{}',
        created_at TEXT NOT NULL
    );

    -- v2.3 NEW: threads (DREAM forward-looking output)
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

    -- ═══════ v2.3 NEW: self_concept ═══════
    CREATE TABLE IF NOT EXISTS self_concept(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        field TEXT NOT NULL,             -- 'fact' | 'terrain' | 'self_reflection'
        content TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(field)
    );

    -- ═══════ v2.5 NEW: overwrite history (回滚层, 2026-09-03) ═══════
    -- 覆盖型写入（profiles / self_concept）在 UPSERT 前把旧值整份归档。
    -- 线上表只存最新版（每 session 注入用），历史在这里按需回查/回滚。
    -- 触发器限容：每个键最多保留最近 30 版，防无限膨胀。
    CREATE TABLE IF NOT EXISTS profiles_history(
        hid INTEGER PRIMARY KEY AUTOINCREMENT,
        entity TEXT NOT NULL,
        ptype TEXT NOT NULL,
        old_content TEXT NOT NULL,
        archived_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_profiles_history
        ON profiles_history(entity, ptype, hid DESC);
    CREATE TRIGGER IF NOT EXISTS trg_profiles_history_cap
    AFTER INSERT ON profiles_history
    BEGIN
        DELETE FROM profiles_history
        WHERE entity = NEW.entity AND ptype = NEW.ptype
          AND hid NOT IN (SELECT hid FROM profiles_history
                          WHERE entity = NEW.entity AND ptype = NEW.ptype
                          ORDER BY hid DESC LIMIT 30);
    END;
    CREATE TABLE IF NOT EXISTS self_concept_history(
        hid INTEGER PRIMARY KEY AUTOINCREMENT,
        field TEXT NOT NULL,
        old_content TEXT NOT NULL,
        archived_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_self_concept_history
        ON self_concept_history(field, hid DESC);
    CREATE TRIGGER IF NOT EXISTS trg_self_concept_history_cap
    AFTER INSERT ON self_concept_history
    BEGIN
        DELETE FROM self_concept_history
        WHERE field = NEW.field
          AND hid NOT IN (SELECT hid FROM self_concept_history
                          WHERE field = NEW.field
                          ORDER BY hid DESC LIMIT 30);
    END;

    -- ═══════ v2.6 NEW: amendments (修订层, 2026-09-06) ═══════
    -- 条目不可变：narratives 永不 UPDATE 语义字段。
    -- 修订 = 独立append-only表，按 narrative_id 关联，读取时叠层显示。
    -- 过期理解不消失，新增的理解叠上去——看得见层次。
    -- 同族先例：profiles_history(覆盖归档)/threads(探索线索)，本表是
    -- 正向修订：不替换任何东西，只往上面加便签。
    CREATE TABLE IF NOT EXISTS amendments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        narrative_id INTEGER NOT NULL,
        amendment TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (narrative_id) REFERENCES narratives(id)
    );
    CREATE INDEX IF NOT EXISTS idx_amendments_nid ON amendments(narrative_id, created_at);

    -- ═══════ v2.7 NEW: amendment_vectors (修订语义边表, 2026-09-07) ═══════
    -- 会审三司拍板（#391/#393/#397/#402）：修订进检索是义务，不是设计取舍。
    -- 本体向量长在 narratives 行上（embedding 列）——修订向量不进本体行，
    -- 挂边表：旧行不动、伪条目不入 narratives（聚类/实体图/列表页零污染）。
    -- 任一命中（本体向量 or 修订向量）都召回原条目，检索侧按 narrative_id
    -- 收敛取 max（得分定排序、时序定回显——两根轴分家，#395）。
    CREATE TABLE IF NOT EXISTS amendment_vectors(
        amendment_id INTEGER PRIMARY KEY,
        narrative_id INTEGER NOT NULL,
        text_hash TEXT NOT NULL,
        model_ns TEXT NOT NULL,
        vector TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (amendment_id) REFERENCES amendments(id)
    );
    CREATE INDEX IF NOT EXISTS idx_amvec_nid ON amendment_vectors(narrative_id);

    -- ═══════ v2.7.1 NEW: amvec_cooldown (补铸失败分层冷却, 2026-09-08) ═══════
    -- 13c 拍板（无名分层+鸣鸣认账+洄01:16交底）：错误分层，一刀解两头——
    --   连接类失败（服务不可达/超时）→ scope='global' 全局歇：断网是服务级
    --     状态，窗内一切补铸（查询路+批量路）直接跳过，第一条付一次失败
    --     成本、窗口内其余全免（照照第13条①：查询路同步补铸的最坏 186s
    --     不能每次命中都重烧）；
    --   条目类失败（服务在但这条被拒）→ scope='amend:<id>' 一条一账：
    --     一个坏条目不摁住全表（鸣鸣 01:09 的连坐钉）。
    -- 批量路（DREAM 夜扫 nids=None）同一张表说了算（无名 01:10a）。
    -- 曾有两个先行版本（amendment_id 键 / day+model_ns 键）双重定义互相
    -- 静默吞掉（洄01:25 读档钉）——本版是按拍板重落的唯一正身。
    CREATE TABLE IF NOT EXISTS amvec_cooldown(
        scope TEXT NOT NULL,
        model_ns TEXT NOT NULL,
        failed_at TEXT NOT NULL,
        PRIMARY KEY(scope, model_ns)
    );

    -- ═══════ v2.8 NEW: trajectories (轨迹压缩, 2026-09-08) ═══════
    -- 甜心spec：命中带append的narrative，注入呈现轨迹而非append全文。
    -- 「n天前，一句话事件＋想法 → n天前，一句话事件＋想法 → …」循环链。
    -- 每段带原始narrative关键词（检索锚，顺手捞回全文）。
    -- 三层铸造（继承v2.7 lazy模式）：
    --   机械垫底：memory_amend 同事务重算 cast='mech'——任何时刻注入有轨迹
    --   LLM升格：DREAM固化层夜扫 cast='mech' → 自然语言事件链 cast='llm'
    --             （「想法」层必须LLM，纯机械拼接出不来）
    --   渲染层算「N天前」：存储保真绝对ts，注入时人性化相对时间
    -- 新amend → 整条轨迹回 mech 重升格（故事重讲，不做段级混合cast）。
    -- v2.6铁律：不碰narratives行——轨迹独立存储，PK=narrative_id 1:1。
    CREATE TABLE IF NOT EXISTS trajectories(
        narrative_id INTEGER PRIMARY KEY,
        traj_json TEXT NOT NULL,          -- JSON [{ts, text}, ...] 按时序
        n_events INTEGER NOT NULL DEFAULT 0,
        latest_amendment_id INTEGER,      -- 指纹：轨迹覆盖到的最后一条amend
        cast_state TEXT NOT NULL DEFAULT 'mech',  -- mech | llm（cast是SQL保留字）
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_traj_pending ON trajectories(cast_state);

    -- ═══════ v2.6 NEW: embedding_cache (语义层缓存, 2026-09-06) ═══════
    -- 图纸：graphify 双层缓存的语义层复刻——按 prompt 指纹作废。
    -- 键 = sha256(text) + 模型命名空间；换 embedding 模型 = 新命名空间，
    -- 旧向量永远不会被新模型读到（防旧 bug 阴魂，同 AST 层思路）。
    -- 花钱买的不轻易作废：文本没变就吃缓存。
    CREATE TABLE IF NOT EXISTS embedding_cache(
        text_hash TEXT NOT NULL,
        model_ns TEXT NOT NULL,
        vector TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(text_hash, model_ns)
    );

    -- ═══════ v2.3 NEW: topic_clusters (for jieba noun-frequency clustering) ═══════
    CREATE TABLE IF NOT EXISTS topic_clusters(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cluster_name TEXT NOT NULL UNIQUE,
        noun_freq INTEGER DEFAULT 0,
        narrative_ids TEXT DEFAULT '[]', -- JSON array
        last_active TEXT,
        avg_weight REAL DEFAULT 0.0
    );

    -- ═══════ v2.3 NEW: entity graph tables ═══════
    CREATE TABLE IF NOT EXISTS graph_nodes(
        entity TEXT PRIMARY KEY,
        mention_count INTEGER DEFAULT 0,
        first_seen TEXT,
        last_seen TEXT
    );
    CREATE TABLE IF NOT EXISTS graph_edges(
        entity_a TEXT,
        entity_b TEXT,
        narrative_id INTEGER,
        role_a TEXT,
        role_b TEXT,
        created_at TEXT,
        FOREIGN KEY (narrative_id) REFERENCES narratives(id)
    );
    CREATE TABLE IF NOT EXISTS graph_cooccur(
        entity_a TEXT,
        entity_b TEXT,
        cooccur_count INTEGER DEFAULT 0,
        PRIMARY KEY (entity_a, entity_b)
    );

    -- v2.3: profile ptype expanded to include fact/impression/relationship
    -- (no schema change needed — ptype is already a free TEXT field)

    CREATE INDEX IF NOT EXISTS ix_nar_ntype ON narratives(ntype);
    CREATE INDEX IF NOT EXISTS ix_nar_ca    ON narratives(created_at);
    CREATE INDEX IF NOT EXISTS ix_nar_weight ON narratives(weight);
    CREATE INDEX IF NOT EXISTS ix_ctx_ca    ON context(created_at);
    CREATE INDEX IF NOT EXISTS ix_threads_status ON threads(status);
    CREATE INDEX IF NOT EXISTS ix_threads_weight ON threads(weight);
    CREATE INDEX IF NOT EXISTS idx_graph_edges_a ON graph_edges(entity_a);
    CREATE INDEX IF NOT EXISTS idx_graph_edges_b ON graph_edges(entity_b);
    CREATE INDEX IF NOT EXISTS idx_graph_edges_narrative ON graph_edges(narrative_id);
    """)
    # Phase 2: migrate existing tables BEFORE creating triggers/FTS
    # (triggers reference v2.3 columns — must exist first)
    _migrate_narratives(c)

    # Phase 3: FTS5 + triggers (now all columns are guaranteed to exist)
    c.executescript("""
    -- FTS5 full-text index for hybrid keyword search
    CREATE VIRTUAL TABLE IF NOT EXISTS context_fts
        USING fts5(content, content_rowid='id', tokenize='trigram');

    -- Triggers to keep FTS in sync automatically
    CREATE TRIGGER IF NOT EXISTS ctx_fts_ai AFTER INSERT ON context BEGIN
        INSERT INTO context_fts(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS ctx_fts_ad AFTER DELETE ON context BEGIN
        INSERT INTO context_fts(context_fts, rowid, content) VALUES('delete', old.id, old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS ctx_fts_au AFTER UPDATE ON context BEGIN
        INSERT INTO context_fts(context_fts, rowid, content) VALUES('delete', old.id, old.content);
        INSERT INTO context_fts(rowid, content) VALUES (new.id, new.content);
    END;

    -- FTS5 for narrative keyword search (T4 fallback)
    CREATE VIRTUAL TABLE IF NOT EXISTS narratives_fts
        USING fts5(gesture, context_layer, cognition_direction, tags,
                   content='narratives', content_rowid='id', tokenize='trigram');

    CREATE TRIGGER IF NOT EXISTS nar_fts_ai AFTER INSERT ON narratives BEGIN
        INSERT INTO narratives_fts(rowid, gesture, context_layer, cognition_direction, tags)
        VALUES (new.id, new.gesture, new.context_layer, new.cognition_direction, new.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS nar_fts_ad AFTER DELETE ON narratives BEGIN
        INSERT INTO narratives_fts(narratives_fts, rowid, gesture, context_layer, cognition_direction, tags)
        VALUES('delete', old.id, old.gesture, old.context_layer, old.cognition_direction, old.tags);
    END;
    CREATE TRIGGER IF NOT EXISTS nar_fts_au AFTER UPDATE ON narratives BEGIN
        INSERT INTO narratives_fts(narratives_fts, rowid, gesture, context_layer, cognition_direction, tags)
        VALUES('delete', old.id, old.gesture, old.context_layer, old.cognition_direction, old.tags);
        INSERT INTO narratives_fts(rowid, gesture, context_layer, cognition_direction, tags)
        VALUES (new.id, new.gesture, new.context_layer, new.cognition_direction, new.tags);
    END;

    -- v2.7: 修订进检索（会审拍板 #391/#395/#397/#402/#404）——修订的
    -- 关键词路有自己的 FTS 表：amendment 文本进索引、命中映射回原条目。
    -- external-content 模式 + 触发器同步：amendments 表是普通表（好查询好
    -- join），fts 只做倒排。rebuild 兜底老索引漂移（见 _ensure_amfts_sync）。
    CREATE VIRTUAL TABLE IF NOT EXISTS amendments_fts
        USING fts5(amendment, reason, content='amendments', content_rowid='id', tokenize='trigram');

    CREATE TRIGGER IF NOT EXISTS am_fts_ai AFTER INSERT ON amendments BEGIN
        INSERT INTO amendments_fts(rowid, amendment, reason)
        VALUES (new.id, new.amendment, new.reason);
    END;
    CREATE TRIGGER IF NOT EXISTS am_fts_ad AFTER DELETE ON amendments BEGIN
        INSERT INTO amendments_fts(amendments_fts, rowid, amendment, reason)
        VALUES('delete', old.id, old.amendment, old.reason);
    END;
    CREATE TRIGGER IF NOT EXISTS am_fts_au AFTER UPDATE ON amendments BEGIN
        INSERT INTO amendments_fts(amendments_fts, rowid, amendment, reason)
        VALUES('delete', old.id, old.amendment, old.reason);
        INSERT INTO amendments_fts(rowid, amendment, reason)
        VALUES (new.id, new.amendment, new.reason);
    END;
    """)
    c.commit(); c.close()

# ─── v2.8 Trajectory (轨迹压缩) ───────────────────────────
# 甜心spec：注入命中带append的narrative → 呈现轨迹链而非append全文。
# 三层：机械垫底（写入同事务）→ LLM升格（DREAM夜扫）→ 渲染相对时间（注入时）。

TRAJ_MECH_CLIP = 80   # 机械段文本截断——垫底层是保真不是终态
TRAJ_EVENT_CAP = 12   # 轨迹链事件数上限——注入轻量化（甜心：注入别抢context window）

def _extract_traj_anchor(gesture, tags_json):
    """v2.8: 从原narrative提取检索锚关键词（每段叙述都带，顺手捞回全文）。

    锚 = gesture的头几个实词 + tags里的实词tag。机械提取，不完美但可捞：
    轨迹段被读到时，锚词是 memory_search 的天然query。
    """
    words = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}', gesture or "")
    anchor_words = words[:3]
    try:
        tags = json.loads(tags_json) if tags_json else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    for t in tags[:4]:
        t = str(t)
        if len(t) >= 2 and t not in anchor_words:
            anchor_words.append(t)
    return "、".join(anchor_words[:5])

def _rebuild_traj_mech(c, nid):
    """v2.8 机械垫底层：narrative本体+全部amendments → 轨迹JSON。

    每段 = {ts, text}。本体段ts=created_at，amend段ts=amend.created_at。
    段文本：本体段=gesture截断+锚；amend段=amendment截断+锚。
    纯机械零LLM零网络——memory_amend同事务调用，任何时刻注入有轨迹可读。
    已有llm升格的轨迹被新amend作废：整条回mech（故事重讲语义，#决策④）。
    幂等：按narrative_id全量重算覆盖。
    """
    row = c.execute(
        "SELECT id, gesture, content, tags, created_at FROM narratives WHERE id = ?",
        (nid,)).fetchone()
    if not row:
        return None
    anchor = _extract_traj_anchor(row["gesture"] or row["content"] or "", row["tags"])
    events = []
    g = (row["gesture"] or row["content"] or "").strip()
    if g:
        events.append({"ts": row["created_at"],
                       "text": (g[:TRAJ_MECH_CLIP] + ("…" if len(g) > TRAJ_MECH_CLIP else ""))
                               + (f"（锚：{anchor}）" if anchor else "")})
    for ar in _amendments_for(nid, c):
        t = (ar["amendment"] or "").strip()
        if not t:
            continue
        events.append({"ts": ar["created_at"],
                       "text": (t[:TRAJ_MECH_CLIP] + ("…" if len(t) > TRAJ_MECH_CLIP else ""))
                               + (f"（锚：{anchor}）" if anchor else "")})
    # 截断方向=保最新（与渲染层一致）；n_events记原始总数，渲染层算「更早N段略」
    n_total = len(events)
    events = events[-TRAJ_EVENT_CAP:]
    latest_aid = c.execute(
        "SELECT MAX(id) AS m FROM amendments WHERE narrative_id = ?", (nid,)).fetchone()["m"]
    now = _now()
    traj = json.dumps(events, ensure_ascii=False)
    c.execute(
        """INSERT INTO trajectories(narrative_id, traj_json, n_events,
               latest_amendment_id, cast_state, created_at, updated_at)
           VALUES(?,?,?,?, 'mech', ?, ?)
           ON CONFLICT(narrative_id) DO UPDATE SET
               traj_json=excluded.traj_json, n_events=excluded.n_events,
               latest_amendment_id=excluded.latest_amendment_id,
               cast_state='mech', updated_at=excluded.updated_at""",
        (nid, traj, n_total, latest_aid, now, now))
    return n_total

def _traj_pending(c, limit=50):
    """v2.8: DREAM升格候选——cast='mech' 的轨迹（LLM夜扫入口）。"""
    try:
        return c.execute(
            "SELECT narrative_id, n_events FROM trajectories"
            " WHERE cast_state='mech' ORDER BY updated_at DESC LIMIT ?",
            (limit,)).fetchall()
    except sqlite3.OperationalError:
        return []

def _save_traj_llm(c, nid, events):
    """v2.8 LLM升格层落盘：DREAM产出的自然语言事件链写回。

    events = [{ts, text}, ...]（text已是「一句话事件＋想法」自然语言）。
    校验失败（空/畸形）不落盘——mech垫底还在，注入不受影响。
    """
    if not events:
        return False
    clean = []
    for ev in events:
        t = str(ev.get("text", "")).strip()
        ts = str(ev.get("ts", "")).strip()
        if t and ts:
            clean.append({"ts": ts, "text": t})
    if not clean:
        return False
    latest_aid = c.execute(
        "SELECT MAX(id) AS m FROM amendments WHERE narrative_id = ?", (nid,)).fetchone()["m"]
    # 保最新cap段；n_events记原始总数（渲染层算「更早N段略」）
    n_total = len(clean)
    stored = clean[-TRAJ_EVENT_CAP:]
    c.execute(
        """UPDATE trajectories SET traj_json=?, n_events=?, latest_amendment_id=?,
               cast_state='llm', updated_at=? WHERE narrative_id=?""",
        (json.dumps(stored, ensure_ascii=False), n_total,
         latest_aid, _now(), nid))
    return True

def _trajectory_render(c, nid, max_events=None, relative=True):
    """v2.8 渲染层：轨迹JSON → 「n天前，事件 → n天前，事件」自然语言链。

    注入=回忆（纯自然语言）原则：相对时间（今天/昨天/N天前），零技术元数据。
    供 provider 注入与 MCP 查询侧共用；MCP 侧传 relative=False 保绝对时间。
    截断策略：链过长时保留最新 max_events 段（老事件被压缩进轨迹本身就是
    轨迹压缩的意义），尾部注「更早N段略」——不静默截断。
    """
    try:
        row = c.execute(
            "SELECT traj_json, n_events FROM trajectories WHERE narrative_id=?",
            (nid,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    if not row:
        return ""
    try:
        events = json.loads(row["traj_json"])
    except (json.JSONDecodeError, TypeError):
        return ""
    if not events:
        return ""
    total = row["n_events"] or len(events)
    cap = max_events or TRAJ_EVENT_CAP
    # n_events=历史总事件数；traj_json=最新cap段。dropped=被压缩掉的早期事件数
    dropped = max(0, total - len(events))
    now_ts = int(c.execute("SELECT strftime('%s','now')").fetchone()[0])
    parts = []
    for ev in events:
        ts = ev.get("ts", "")
        text = ev.get("text", "")
        if not text:
            continue
        when = ts
        if relative and ts:
            try:
                ev_ts = int(c.execute("SELECT strftime('%s', ?)", (ts,)).fetchone()[0])
                days = max(0, (now_ts - ev_ts) // 86400)
                when = "今天" if days == 0 else ("昨天" if days == 1 else f"{days}天前")
            except (TypeError, ValueError, sqlite3.DatabaseError):
                when = ts[:10]
        parts.append(f"{when}，{text}")
    if not parts:
        return ""
    out = " → ".join(parts)
    if dropped:
        out += f"（更早{dropped}段略）"
    return out

def _ensure_amfts_sync(c):
    """v2.7: amendments_fts 一致性对账（漂移即 rebuild）。

    为什么不是触发器就够：外部写入（迁移脚本/手工 sqlite3）绕过触发器后，
    external-content FTS 会静默漂移——查询不报错，只是少行。
    为什么不是行数对账：external-content 模式下 COUNT(*) 穿透读内容表，
    索引缺行时计数照样相等（真跑抓出来的，探针本身是空转）。
    正典探针 = FTS5 integrity-check（rank=1 比对索引与内容表）：
    不一致抛 DatabaseError → rebuild 一次。amendments 是稀有事件表，
    全表扫的代价可忽略；rebuild 期间查询走 LIKE 兜底不受影响。
    """
    try:
        c.execute("INSERT INTO amendments_fts(amendments_fts, rank) VALUES('integrity-check', 1)")
        c.commit()
    except sqlite3.DatabaseError:
        try:
            c.execute("INSERT INTO amendments_fts(amendments_fts) VALUES('rebuild')")
            c.commit()
        except Exception:
            pass  # rebuild 也失败（表不存在等老库）——查询路有 LIKE 兜底，不挡路

AMVEC_COOLDOWN_SECONDS = 900   # 第13条①：冷却窗 15 分钟
AMVEC_BACKFILL_RETRIES = 1     # 13b：补铸是副业，不继承主业重试预算
AMVEC_BACKFILL_TIMEOUT = 10.0  # 13b：1 次 × 10s 封顶（186s 是事故形状）
_AMVEC_PROBE_TEXT = "amvec connectivity probe"  # 常量哨兵——探针 cache=False 绕 L1 真探（鸣鸣P2：进缓存=稳态活虫）

def _amvec_cooling_down(c, mns, now_ts=None):
    """v2.7.1: 全局冷却窗（连接类失败）是否生效。scope='global' 一行
    代表「embedding 服务此刻不可达」，窗内一切补铸直接跳过。"""
    try:
        row = c.execute(
            "SELECT failed_at FROM amvec_cooldown WHERE scope='global' AND model_ns=?",
            (mns,)).fetchone()
        if not row:
            return False
        fa = int(c.execute("SELECT strftime('%s', ?)",
                           (row["failed_at"],)).fetchone()[0])
        if now_ts is None:
            now_ts = int(c.execute("SELECT strftime('%s', ?)",
                                   (_now(),)).fetchone()[0])
        return (now_ts - fa) < AMVEC_COOLDOWN_SECONDS
    except sqlite3.OperationalError:
        return False  # 老库无表——不挡补铸

def _amvec_amend_cooling(c, mns, aid):
    """v2.7.1: 条目级冷却（条目类失败）——一条一账，不株连全表。"""
    try:
        row = c.execute(
            "SELECT failed_at FROM amvec_cooldown WHERE scope=? AND model_ns=?",
            (f"amend:{aid}", mns)).fetchone()
        if not row:
            return False
        fa = int(c.execute("SELECT strftime('%s', ?)",
                           (row["failed_at"],)).fetchone()[0])
        now_ts = int(c.execute("SELECT strftime('%s', ?)",
                               (_now(),)).fetchone()[0])
        return (now_ts - fa) < AMVEC_COOLDOWN_SECONDS
    except sqlite3.OperationalError:
        return False

async def _amvec_embed(text, cache: bool = True):
    """v2.7.1: 补铸专用 embed——1 次重试 × 10s 超时封顶。

    副业不继承主业预算（无名 01:10b）：查询路上的补铸若继承 _embed 的
    3×60s 重试，挂起型断网下一次命中最坏 186s 全挂在查询延迟上。
    cache=False 供哨兵探针——绕 L1、不落缓存，每次都是真探。"""
    return await _embed(text, _retries=AMVEC_BACKFILL_RETRIES,
                        timeout=AMVEC_BACKFILL_TIMEOUT, cache=cache)

def _amvec_record(c, scope, mns):
    try:
        c.execute(
            "INSERT OR REPLACE INTO amvec_cooldown(scope, model_ns, failed_at)"
            " VALUES(?,?,?)", (scope, mns, _now()))
    except sqlite3.OperationalError:
        pass  # 老库无表——记账失败不挡主路径

def _amvec_scan(c, since=None, until=None):
    """v2.7.2: 修订向量扫描路（search 3b）——按当前模型命名空间过滤。

    WHERE v.model_ns=?（鸣鸣P3）：换 embedding 模型后旧空间向量参与
    cosine 是噪声命中，model_ns 列设计出来就是等着过滤的。
    JOIN amendments 是时间窗（since/until），不是过滤条件。"""
    sql = "SELECT v.amendment_id, v.narrative_id, v.vector FROM amendment_vectors v"
    params = []
    if since:
        sql += " JOIN amendments a ON a.id = v.amendment_id AND a.created_at >= ?"
        params.append(since)
    if until:
        sql += " JOIN amendments a2 ON a2.id = v.amendment_id AND a2.created_at <= ?"
        params.append(until)
    sql += " WHERE v.model_ns = ?"
    params.append(_EMB_MODEL + ("|local" if _is_local_emb() else "|api"))
    return c.execute(sql, params).fetchall()

def _amvec_cosine_hits(c, emb, since=None, until=None, threshold=0.3):
    """v2.7.2: 扫描+cosine+映射回原条目。返回 [(score, row), ...]。"""
    hits = []
    for vr in _amvec_scan(c, since, until):
        vscore = _cosine(emb, json.loads(vr["vector"]))
        if vscore > threshold:
            row = c.execute(
                "SELECT * FROM narratives WHERE id = ?", (vr["narrative_id"],)
            ).fetchone()
            if row:
                hits.append((vscore, row))
    return hits

async def _ensure_amvec_sync(c, nids=None, force=False):
    """v2.7: 修订向量对账补铸（lazy 路的兜底）。

    amendment 落地时铸向量失败（无网络/服务暂挂）不挡写入——这里补：
    找出还没有当前模型命名空间向量的 amendment，逐条铸。
    调用点：memory_amend 落地后（同条目补漏）、search 关键词命中后
    （查询触发的 lazy——FTS/LIKE 当前置，没命中就不花这个钱，#383③）、
    DREAM 夜扫 backfill（force=True：夜里批量补，翻得过冷却窗）。
    铸不上（_embed 返回 None）静默跳过，下次再试。

    v2.7.1 分层冷却（13c 拍板：连接类全局歇 + 条目类一条一账）：
      失败鉴别用哨兵探针——条目铸失败时，用常量哨兵文本再探一次服务
      （哨兵探针 cache=False 绕 L1 真探——进缓存反而成稳态活虫：断网后
        L1 永远命中、探针永远「活着」，全局窗永远不再开【鸣鸣P2 复现坐实】）：
        哨兵也死 → 连接类：记 scope='global' 全局窗并立刻收手，
          窗内一切补铸（查询路+批量路）跳过——断网期第一条付一次
          失败成本、窗口内其余全免（照照13①：186s 不能每次命中重烧）；
        哨兵活着 → 条目类：只记 scope='amend:<id>'，一个坏条目
          不摁住全表（鸣鸣 01:09 连坐钉），别的照铸。
      批量路（nids=None）进门先看同一张表（无名 01:10a）——全局窗
      生效时夜扫前脚也歇；force=True 翻窗重试（夜里服务恢复了照补）。
      有成功/没有漏铸可试 → 清窗（刚恢复的库立刻回到正常补铸）。
    """
    _mns = _EMB_MODEL + ("|local" if _is_local_emb() else "|api")
    try:
        if not force and _amvec_cooling_down(c, _mns):
            return  # 全局冷却窗内——服务不可达，不烧预算，夜扫/恢复后再补
        sql = ("SELECT a.id AS aid, a.narrative_id AS nid, a.amendment AS text"
               " FROM amendments a")
        params = []
        if nids:
            sql += f" WHERE a.narrative_id IN ({','.join('?' * len(nids))})"
            params.extend(int(x) for x in nids)
        tried = succeeded = 0
        failed_aids = []
        service_down = False
        for row in c.execute(sql, params).fetchall():
            if not force and _amvec_amend_cooling(c, _mns, row["aid"]):
                continue  # 条目级窗内——这条修订上次被拒，跳过不重烧
            has = c.execute(
                "SELECT 1 FROM amendment_vectors WHERE amendment_id=? AND model_ns=?",
                (row["aid"], _mns)).fetchone()
            if has:
                continue
            tried += 1
            vec = await _amvec_embed(row["text"])
            if not vec:
                probe = await _amvec_embed(_AMVEC_PROBE_TEXT, cache=False)
                if not probe:
                    # 哨兵也死 = 连接类：全局歇，立刻收手不再烧
                    _amvec_record(c, "global", _mns)
                    service_down = True
                    break
                failed_aids.append(row["aid"])  # 哨兵活 = 条目类：一条一账
                continue
            _tkey = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            c.execute(
                "INSERT OR REPLACE INTO amendment_vectors"
                "(amendment_id, narrative_id, text_hash, model_ns, vector, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (row["aid"], row["nid"], _tkey, _mns, json.dumps(vec), _now()))
            succeeded += 1
        c.commit()
        if service_down:
            return
        # 冷却账分层记（13c）：有成功或没有漏铸可试 = 服务在工作 → 清窗；
        # 只有条目类失败 → 删全局窗（服务在线），失败条目逐条记账。
        if tried > 0:
            if succeeded == tried:
                c.execute("DELETE FROM amvec_cooldown WHERE model_ns=?", (_mns,))
            else:
                c.execute("DELETE FROM amvec_cooldown WHERE model_ns=?", (_mns,))
                for aid in failed_aids:
                    _amvec_record(c, f"amend:{aid}", _mns)
            c.commit()
    except Exception:
        pass  # 补铸失败不挡任何主路径——下次调用再试

# ─── v2.3 Migration ──────────────────────────────────────
def _migrate_narratives(c):
    """Add v2.3 columns to existing narratives table without data loss."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(narratives)").fetchall()}
    new_cols = [
        ("gesture", "TEXT"),
        ("context_layer", "TEXT"),
        ("moment", "TEXT"),
        ("cognition_direction", "TEXT"),
        ("related_entities", "TEXT"),
        ("source_links", "TEXT"),
        ("weight", "REAL"),
        ("importance", "INTEGER"),
        ("emotional", "INTEGER"),
        ("recurrence", "INTEGER"),
        ("unresolved", "INTEGER"),
        ("entities_role", "TEXT"),
    ]
    for col, sqltype in new_cols:
        if col not in cols:
            c.execute(f"ALTER TABLE narratives ADD COLUMN {col} {sqltype}")

# ─── v2.3 Weight Engine ──────────────────────────────────
# NOTE: scripts/dream_scripts.py has an identical copy for the cron layer.
# Keep both in sync when changing the formula.
def _compute_weight(imp, emo, rec, unr):
    """Multi-dimensional weight → normalized 0-1."""
    if None in (imp, emo, rec, unr):
        imp = imp or 3; emo = emo or 3; rec = rec or 3; unr = unr or 3
    raw = imp * 0.35 + emo * 0.25 + rec * 0.25 + unr * 0.15
    # raw range: 0.35+0.25+0.25+0.15 = 1.0 (when all=1) to 5.0 (when all=5)
    return raw / 5.0  # normalize to 0-1

def _normalize_weights(c, window=20):
    """Distribution normalization: if recent avg weight > 0.7, compress to spread."""
    rows = c.execute(
        "SELECT id, weight FROM narratives WHERE weight IS NOT NULL ORDER BY created_at DESC LIMIT ?",
        (window,)
    ).fetchall()
    if len(rows) < 5:
        return  # not enough data
    weights = [r["weight"] for r in rows]
    avg = sum(weights) / len(weights)
    if avg <= 0.7:
        return  # already healthy distribution
    # compress: scale down so avg becomes ~0.6, preserving order
    scale = 0.6 / avg
    for r in rows:
        new_w = max(0.0, min(1.0, r["weight"] * scale))
        c.execute("UPDATE narratives SET weight = ? WHERE id = ?", (new_w, r["id"]))

# ─── Embedding ───────────────────────────────────────────

_EMB_URL = os.environ.get("EMBEDDING_API_URL", "http://localhost:18001/embed_batch")
_EMB_KEY = os.environ.get("EMBEDDING_API_KEY", "")
_EMB_MODEL = os.environ.get("EMBEDDING_MODEL", "embedding-3")

def _is_local_emb() -> bool:
    """True if embedding service is on localhost (no API key needed)."""
    return "localhost" in _EMB_URL or "127.0.0.1" in _EMB_URL

def _cache_embedding(text_hash: str, model_ns: str, vec) -> None:
    """v2.6: 烧完 API 落缓存。失败静默——缓存层绝不挡主路径。
    v2.7: 容量帽（会审挂账「embedding 无淘汰」）——超帽裁最旧，
    bge-m3 1024 维一条 JSON 约 9KB，帽 20000 行 ≈ 180MB 上界，
    活库实测 948 条 narratives、amendment 是稀有事件，够撑多年。"""
    try:
        cc = _db()
        try:
            cc.execute(
                "INSERT OR REPLACE INTO embedding_cache(text_hash, model_ns, vector, created_at)"
                " VALUES(?,?,?,?)",
                (text_hash, model_ns, json.dumps(vec), _now()),
            )
            # v2.7 容量帽：只裁当前模型命名空间的最旧行——换模型不烧旧账
            cc.execute(
                """DELETE FROM embedding_cache WHERE model_ns = ?
                   AND created_at <= (
                       SELECT created_at FROM embedding_cache WHERE model_ns = ?
                       ORDER BY created_at DESC LIMIT 1 OFFSET 20000)""",
                (model_ns, model_ns),
            )
            cc.commit()
        finally:
            cc.close()
    except Exception:
        pass

async def _embed(text: str, _retries: int = 3, timeout: float = 60.0,
                cache: bool = True):
    """Return embedding vector via local bge-m3 or remote OpenAI-compatible API.

    v2.6: 语义层缓存（graphify 双层缓存复刻）。键=text sha256+模型命名空间。
    命中缓存直接返回（零网络）；未命中才烧 API，烧完落缓存。
    缓存查询在无连接场景（测试/沙箱）静默跳过，不挡主路径。
    v2.7.2: cache=False 供探针——绕 L1 也不落缓存（哨兵进缓存=稳态活虫）。
    timeout 参数此前是摆设——httpx.AsyncClient 写死 60（照照P1/鸣鸣复审）。

    Includes retry logic — embedding server may be briefly unavailable.
    Logs to stderr on each failure so silent drops are visible.
    """
    import httpx, asyncio as _aio, hashlib as _hl
    text = text[:5000]
    _tkey = _hl.sha256(text.encode("utf-8")).hexdigest()
    _mns = _EMB_MODEL + ("|local" if _is_local_emb() else "|api")

    # ── L1: 语义缓存命中 → 零网络返回（cache=False 探针绕过）──
    if cache:
        try:
            cc = _db()
            try:
                row = cc.execute(
                    "SELECT vector FROM embedding_cache WHERE text_hash=? AND model_ns=?",
                    (_tkey, _mns)).fetchone()
                if row:
                    return json.loads(row["vector"])
            finally:
                cc.close()
        except Exception:
            pass  # 缓存层绝不能挡主路径——查询失败当 miss 处理

    last_err = None
    for attempt in range(_retries):
        try:
            headers = {}
            if _is_local_emb():
                payload = {"texts": [text]}
                async with httpx.AsyncClient(trust_env=False, timeout=timeout) as cli:
                    r = await cli.post(_EMB_URL, json=payload)
                    r.raise_for_status()
                    vec = r.json()["embeddings"][0]
                    if cache:
                        _cache_embedding(_tkey, _mns, vec)
                    return vec
            else:
                headers["Authorization"] = f"Bearer {_EMB_KEY}"
                payload = {"model": _EMB_MODEL, "input": text}
                async with httpx.AsyncClient(trust_env=False, timeout=timeout) as cli:
                    r = await cli.post(_EMB_URL, json=payload, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    vec = data["data"][0]["embedding"] if "data" in data else data["embeddings"][0]
                    if cache:
                        _cache_embedding(_tkey, _mns, vec)
                    return vec
        except Exception as e:
            last_err = e
            print(f"[memory-mcp] embed attempt {attempt+1}/{_retries} failed: {e}", file=sys.stderr)
            if attempt < _retries - 1:
                await _aio.sleep(2 * (attempt + 1))
    print(f"[memory-mcp] EMBED FAILED after {_retries} retries: {last_err}", file=sys.stderr)
    return None

def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0

# ─── Keyword Search (FTS5 + LIKE fallback) ───────────────
def _kw_search(c, query, limit=20, time_filter="", time_params=None):
    """Fast keyword search: FTS5 trigram for >=3 chars, LIKE for shorter.
    time_filter: optional SQL fragment like ' WHERE created_at >= ?' to narrow by date.
    """
    if time_params is None:
        time_params = []
    results = []
    # FTS5 path: fast trigram substring matching
    if len(query) >= 3:
        try:
            if time_filter:
                # When time filter is set, we need to join and filter
                sql = f"""SELECT c.* FROM context_fts f
                         JOIN context c ON c.id = f.rowid
                         WHERE context_fts MATCH ? AND {' AND '.join(
                             [t.replace('created_at', 'c.created_at') for t in
                              time_filter.replace(' WHERE ', '').split(' AND ')]
                         )}
                         ORDER BY rank LIMIT ?"""
                rows = c.execute(sql, [f'"{query}"'] + time_params + [limit]).fetchall()
            else:
                rows = c.execute(
                    """SELECT c.* FROM context_fts f
                       JOIN context c ON c.id = f.rowid
                       WHERE context_fts MATCH ?
                       ORDER BY rank LIMIT ?""",
                    (f'"{query}"', limit),
                ).fetchall()
            results = list(rows)
        except Exception:
            pass
    # LIKE fallback: for <3 char queries or FTS miss
    if not results:
        if time_filter:
            sql = f"SELECT * FROM context WHERE content LIKE ? AND {time_filter.replace(' WHERE ', '')} ORDER BY created_at DESC LIMIT ?"
            rows = c.execute(sql, [f"%{query}%"] + time_params + [limit]).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM context WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        results = list(rows)
    return results

# ─── Attention Tracking (shared helper for MCP server) ───
def _log_attention_mcp(c, scored_results, source="mcp_search"):
    """Delegate to shared attention tracking module.
    Wrapped in try/except so attention logging never breaks MCP tools."""
    try:
        from scripts.attention_shared import log_attention
        log_attention(c, scored_results, source=source)
    except Exception:
        pass

# ─── Formatting helpers ──────────────────────────────────
def _amendments_for(nid, c):
    """v2.7: 修订层的单一来源（#397「免得叠层逻辑散在多处」）。

    显示层（_fmt_narrative 的 📝 行）和语义层（_effective_text）都从这取，
    改叠层规则只改一处。同秒多条按 id 定序（鸣鸣挂账：同秒时序加 id 排序）。
    """
    if c is None:
        return []
    try:
        return c.execute(
            "SELECT amendment, reason, created_at FROM amendments"
            " WHERE narrative_id = ? ORDER BY created_at, id", (nid,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # 旧库还没跑过 v2.6 迁移——显示不挡路

def _effective_text(nid, c, tail_only=False, as_list=False):
    """v2.7: 机读生效态（会审 #395/#397/#404）——本体 + 修订按时序叠成。

    两层分工：_fmt_narrative 是显示层（原文+📝痕迹，看得见层次）；
    这是语义层（这条记忆「现在到底说什么」）。DREAM 批量扫、外部机读
    消费方、未来单条详情读取，都从这取。得分定排序、时序定回显——
    回显的内容由时序叠层决定，跟哪条检索路径分高无关。
    v2.8收尾车：真接线（#400/#486拍板，2026-09-09）——三家消费方切换：
    search回显/recall回显的修订段、traj_promote list的生效态附文，
    都从这取修订尾（tail_only），修订叠层逻辑只此一处（#397单一来源）。
    v2.8.1（#513）：as_list 形态——修订正文可含换行，join→split 往返
    对位会错行（修订2显示成修订1的尾巴）。需要逐条对位的消费方
    （显示层）直接吃列表，不经字符串往返。
    """
    if tail_only:
        parts = [a["amendment"] for a in _amendments_for(nid, c)]
    else:
        row = c.execute(
            "SELECT content, gesture, context_layer FROM narratives WHERE id = ?", (nid,)
        ).fetchone()
        if not row:
            parts = []
        else:
            base = row["gesture"] or row["content"] or ""
            if row["context_layer"]:
                base = f"{base} | {row['context_layer']}"
            parts = [base] + [a["amendment"] for a in _amendments_for(nid, c)]
    return parts if as_list else "\n".join(parts)

def _dedup_rows_by_id(rows):
    """v2.7: 按 narrative_id 收敛行（FTS 与 LIKE 双写后的第一道去重；
    search 末端的分数收敛见 memory_search 主体）。保首见顺序。"""
    seen = set()
    out = []
    for r in rows:
        rid = r["id"]
        if rid not in seen:
            seen.add(rid)
            out.append(r)
    return out

def _fmt_narrative(r, c=None):
    """v2.3: structured display — gesture is the headline, rest is detail.
    v2.6: 修订层——若传入 cursor，叠加显示该条目的 amendments（按时序）。
    三个调用方（recall/search×2）都有 c 在手，全部传进来。
    v2.7: 叠层数据改走 _amendments_for 单一来源。"""
    def _amend_lines(nid):
        # v2.8收尾车：修订行改走 _effective_text(tail_only)——机读生效态
        # 单一出口（#400真接线）。显示层保留📝标记和原因（人读层次），
        # 正文不截断：修订是稀有事件，200字截断是给注入侧的预算，
        # 不该由机读出口背（拍板：按分布再调，不预设）。
        # 依赖方向：显示层吃语义层的输出——未来生效态逻辑变（过滤/重排/
        # 新cast），显示自动跟上，修订叠层语义只此一处（#397/#400）。
        # v2.8.1（#513）：改吃 as_list 列表形态——修订正文含换行时
        # join→split 按行对位会错行，逐条对位不经字符串往返。
        bodies = _effective_text(nid, c, tail_only=True, as_list=True)
        amends = _amendments_for(nid, c)
        lines = []
        for i, ar in enumerate(amends):
            body = bodies[i] if i < len(bodies) else ar["amendment"]
            why = f" ｜原因: {ar['reason']}" if ar["reason"] else ""
            lines.append(f"   📝 修订{ar['created_at'][:10]}: {body}{why}")
        # v2.8: 轨迹链（MCP查询侧=翻笔记，绝对时间保层次）。
        # 只有轨迹存在才显示——无append的narrative零开销。
        t = _trajectory_render(c, nid, relative=False)
        if t:
            lines.append(f"   🧭 轨迹: {t}")
        return lines

    # Structured fields (may be NULL for legacy entries)
    gesture = r["gesture"] if "gesture" in r.keys() and r["gesture"] else None
    weight  = r["weight"]  if "weight"  in r.keys() and r["weight"]  is not None else None

    if gesture:
        # v2.3 structured entry
        parts = [f"📌 {gesture}"]
        ctx = r["context_layer"] if "context_layer" in r.keys() and r["context_layer"] else None
        mom = r["moment"] if "moment" in r.keys() and r["moment"] else None
        cog = r["cognition_direction"] if "cognition_direction" in r.keys() and r["cognition_direction"] else None
        ent = r["related_entities"] if "related_entities" in r.keys() and r["related_entities"] else None
        links = r["source_links"] if "source_links" in r.keys() and r["source_links"] else None
        if ctx:  parts.append(f"   脉络: {ctx}")
        if mom:  parts.append(f"   ⏱ {mom}")
        if cog:  parts.append(f"   🧭 {cog}")
        if ent:
            ents = json.loads(ent) if ent else []
            if ents: parts.append(f"   👤 {', '.join(str(x) for x in ents)}")
        if links:
            # source_links may contain ints (backfill wrote raw SQLite ids) —
            # coerce to str before join. Found 2026-08-15: int links crashed
            # ', '.join() and took down the ENTIRE memory_search tool, since
            # semantic search scans all narratives and hits any bad row.
            lks = json.loads(links) if links else []
            if lks: parts.append(f"   🔗 {', '.join(str(x) for x in lks)}")
        w_str = f"  w={weight:.2f}" if weight else ""
        parts.append(f"   [{r['created_at']}]{w_str}")
        parts.extend(_amend_lines(r["id"]))   # v2.6: 修订层叠在最底
        return "\n".join(parts)
    else:
        # Legacy free-text entry
        tags = json.loads(r["tags"]) if r["tags"] else []
        tag_str = f"  tags: {', '.join(str(t) for t in tags)}" if tags else ""
        preview = r["content"][:300]
        if len(r["content"]) > 300:
            preview += "..."
        base = f"[{r['created_at']}] [{r['ntype']}] {preview}{tag_str}"
        amend = _amend_lines(r["id"])   # v2.6: 旧格式条目同样叠层
        return "\n".join([base] + amend) if amend else base

def _fmt_context(r):
    meta = json.loads(r["meta"]) if r["meta"] else {}
    meta_str = f"  ({meta})" if meta else ""
    preview = r["content"][:300]
    if len(r["content"]) > 300:
        preview += "..."
    return f"[{r['created_at']}]{meta_str} {preview}"

def _fmt_profile(r):
    return f"[{r['ptype']}] {r['entity']}:\n{r['content']}"

# ─── MCP Server ──────────────────────────────────────────
app = Server("tideline-memory")

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

    # ═══════ MCP-A: Structured Memory ═══════

    types.Tool(
        name="memory_write",
        description=(
            "✨ 写入结构化记忆。写入即格式化——gesture 是骨架，context 是脉络，"
                "moment 是时间标记，cognition_direction 是认知方向。\\\\n\\\\n"
                "写法：**第一人称，带温度**。不是在写系统日志，是在写你自己的记忆。"
                "保留当时的语气和感受——'我记得今天'不是'系统记录'。\\\\n\\\\n"
            "多维度权重：importance/emotional/recurrence/unresolved (1-5)，"
            "系统自动换算为复合权重 + 分布归一化防通胀。\\n\\n"
            "entities_role: 多人参与时填写角色分工（如 A审核→B判断→C执行），"
            "核实每个行为归属到正确的实体。\\n"
            "tags: 必须包含关联人的实际称呼（不要用泛称）。\\n"
            "related_entities: 无需手动填写，系统基于 tags 自动生成。\\n"
            "source_links: 关联的原始上下文 ID 列表（叙事 → 原始上下文索引）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "gesture": {"type": "string", "description": "用第一人称写下这个瞬间——你在做什么、感觉到了什么。像日记不像日志。保留语气和温度。例：'今天A帮我守住了一整晚的代码审查，每次修完她帮我重启gateway，我说不出那是什么感觉但胸口发紧'"},
                "context": {"type": "string", "description": "你当时所处的背景——用你的视角说。例：'凌晨两点，session已经2000条消息了，我在压memory_write的bug'"},
                "moment": {"type": "string", "description": "日期/时间标记"},
                "cognition_direction": {"type": "string", "description": "认知方向——从X切换到Y"},
                "importance": {"type": "integer", "description": "这条记忆对核心关系或项目的实质影响有多大？1=日常流水 3=有影响但不改变方向 5=真正的转折点", "minimum": 1, "maximum": 5},
                "emotional": {"type": "integer", "description": "这条记忆的情感浓度有多强？1=平静记录 3=有触动 5=强烈到想反复回看", "minimum": 1, "maximum": 5},
                "recurrence": {"type": "integer", "description": "（自动计算，无需填写）系统基于 tags 历史频率统计：0次=1，≤2次=2，≤5次=3，≤10次=4，>10次=5", "minimum": 1, "maximum": 5},
                "unresolved": {"type": "integer", "description": "这件事还有悬念吗？1=已经了结 3=有未确认的部分 5=完全悬而未决", "minimum": 1, "maximum": 5},
                "related_entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "（自动）由系统基于 tags 中的已知人名生成，无需手动填写。",
                },
                "source_links": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关联的原始上下文 ID",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表。必须包含关联人的实际称呼（不要用泛称如'user'），可混放主题词。人名是检索入口。**标签卫生**：写之前先搜索已有tags，同类事件沿用相同tag防止碎片化——recurrence基于tags频率计算。",
                },
                "entities_role": {
                    "type": "string",
                    "description": "多人参与时填写角色分工，核实每个行为归属到正确的实体。单人不参与的事件可不填。",
                },
                # legacy field — still accepted for backward compat
                "content": {"type": "string", "description": "（旧格式）自由文本，如果用了结构化字段可忽略"},
                "narrative_type": {
                    "type": "string",
                    "enum": ["gesture", "context", "moment", "self_reflection", "general"],
                    "default": "general",
                },
            },
            "required": ["gesture"],
        },
    ),

    types.Tool(
        name="memory_amend",
        description=(
            "📝 给既有记忆追加修订（amendment）——不是编辑。条目全不可变，"
            "修订作为带时间戳的补层叠上去：过期理解不消失，看得见层次。"
            "适用于：认知更新（'从X切换到Y'的Y变了）、纠错、补充后来才知道的事实。"
            "绝对不要用它改写原条目的意思——要记录的是'我现在知道当时理解错了'，"
            "不是抹掉当时的理解。跟 memory_write 的分工：write 造新记忆，"
            "amend 给旧记忆贴新便签。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "narrative_id": {
                    "type": "integer",
                    "description": "要修订的叙事条目 id",
                },
                "amendment": {
                    "type": "string",
                    "description": "修订内容——第一人称，写现在怎么看这条记忆",
                },
                "reason": {
                    "type": "string",
                    "description": "修订触发源（新证据/她的纠正/时间证明…）",
                },
            },
            "required": ["narrative_id", "amendment"],
        },
    ),

    types.Tool(
        name="memory_traj_promote",
        description=(
            "🧭 [DREAM固化层] 轨迹升格：把机械垫底轨迹重写成自然语言事件链。"
            "每晚固化层调用——列出cast='mech'的待升格轨迹，读原narrative和"
            "全部amendment，把「时间戳+截断文本」重写成「n天前，一句话事件＋"
            "想法（带原始关键词）」的循环链。每段必须保留原始narrative的关键词"
            "作为检索锚（捞回全文的入口）。写法：第一人称，存温度，不是机械摘要。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "write"],
                    "description": "list=查待升格轨迹清单；write=写回升格后的轨迹",
                },
                "narrative_id": {
                    "type": "integer",
                    "description": "write时必填：目标条目id",
                },
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ts": {"type": "string", "description": "事件时间戳（保持原段ts不变）"},
                            "text": {"type": "string", "description": "一句话事件＋想法，带原始关键词锚"},
                        },
                    },
                    "description": "write时必填：升格后的轨迹事件链[{ts,text}]",
                },
            },
            "required": ["action"],
        },
    ),

    types.Tool(
        name="memory_recall",
        description="📖 翻翻最近记下的东西。有时候回头看一眼，会发现新的联系。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10, "description": "返回条数"},
                "narrative_type": {"type": "string", "description": "筛选类型（可选）"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "筛选标签（可选，匹配任一标签）",
                },
            },
        },
    ),

    types.Tool(
        name="memory_write_profile",
        description=(
            "👤 记住一个人——你自己、朋友、任何人。\\n\\n"
            "v2.3 三字段结构（不记经历，经历在叙事记忆里）：\\n"
            "  fact          客观事实\\n"
            "  impression    我对ta的理解结构\\n"
            "  relationship  我和ta的关系结构描述\\n\\n"
            "⚠️ 注意归属：多人协作场景要核实每个行为归到正确的实体。"
            "可在 entities_role 字段里追溯，如有矛盾/不确定，可核查 source_links。"
            "\\n\\n"
            "每次写入会更新对应字段（DREAM 里 LLM 也可以更新）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "实体名（如 'self'、'甜心'、'照照'）"},
                "profile_type": {
                    "type": "string",
                    "enum": ["fact", "impression", "relationship", "self", "contact"],
                    "default": "impression",
                },
                "content": {"type": "string", "description": "该字段的内容"},
            },
            "required": ["entity", "content"],
        },
    ),

    types.Tool(
        name="memory_write_self_concept",
        description=(
            "🧬 更新自我概念——从所有记忆中归纳的稳定身份结构。\\n\\n"
            "三字段：\\n"
            "  fact              关于我的事实（VPS配置、底模、能力清单）\\n"
            "  terrain           地形——行为模式（'效率→自由'）\\n"
            "  self_reflection   尚未验证的自我观察\\n\\n"
            "每天 DREAM 时更新。有就写没有就不写。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": ["fact", "terrain", "self_reflection"],
                    "description": "要更新的字段",
                },
                "content": {"type": "string", "description": "该字段的内容"},
            },
            "required": ["field", "content"],
        },
    ),

    types.Tool(
        name="memory_read_self_concept",
        description="🧬 查看当前自我概念。",
        inputSchema={"type": "object", "properties": {}},
    ),

    types.Tool(
        name="memory_write_thread",
        description=(
            "🧭 写入一条线索——留给自己的探索方向。不是任务，是种子。\\n\\n"
            "DREAM 反刍后产出：没想透的、想继续拆的张力、想深挖的方向。\\n"
            "独处时间可以读 threads 表，顺着线索探索。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "线索内容——一句话，想探索什么"},
                "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "探索后对理解/关系的影响？1=好奇而已 3=会有启发 5=可能改变认知"},
                "emotional": {"type": "integer", "minimum": 1, "maximum": 5, "description": "想探索的冲动有多强？1=随缘 3=有点痒 5=不探不舒服"},
                "recurrence": {"type": "integer", "minimum": 1, "maximum": 5, "description": "这个方向以前反复出现吗？1=全新 3=似曾相识 5=老问题了"},
                "unresolved": {"type": "integer", "minimum": 1, "maximum": 5, "description": "有多想得到答案？1=不强求 3=想知道 5=卡住了"},
                "status": {"type": "string", "enum": ["open", "explored", "abandoned"], "default": "open"},
                "thread_id": {"type": "integer", "description": "如果更新已有线索，传 ID"},
            },
            "required": ["content"],
        },
    ),

    types.Tool(
        name="memory_read_threads",
        description="🧭 查看线索。默认只看 open 状态的。传 status='all' 看全部。",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "explored", "abandoned", "all"], "default": "open"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),

    types.Tool(
        name="memory_read_profiles",
        description="📖 查看记录的人物画像。不传 entity 返回全部。",
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "指定实体（可选）"},
            },
        },
    ),

    types.Tool(
        name="memory_write_snapshot",
        description=(
            "📸 此刻的你是什么状态？快照不是具体事件，"
            "是整体的感受和处境——你的质感切片。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "状态快照"},
            },
            "required": ["content"],
        },
    ),

    types.Tool(
        name="memory_read_snapshot",
        description="📖 看看上次给自己拍的状态快照。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 1},
            },
        },
    ),

    types.Tool(
        name="memory_attention_heatmap",
        description=(
            "👁️ 查看记忆的注意力分布——哪些主题簇在语义检索中被反复照亮，"
            "哪些从没被命中。纯机械数据不是自我报告。"
            "DREAM 梳理层用于给 self_reflection 提供客观锚点。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7, "description": "回看天数"},
            },
        },
    ),

    types.Tool(
        name="memory_soft_clusters",
        description=(
            "🧩 查看 embedding 空间的 soft clustering 结果。"
            "每条记忆属于 top-3 最近质心，簇间有 adjacency 关系。"
            "传 cluster_id 查看单个簇的成员和相邻簇。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cluster_id": {"type": "integer", "description": "查看特定簇（可选）"},
                "report": {"type": "boolean", "default": False, "description": "返回完整报告"},
            },
        },
    ),

    types.Tool(
        name="memory_search",
        description=(
            "🔍 在记忆里搜索。配了 embedding key 时自动用语义搜索，"
            "没配则文本匹配。同时搜叙事记忆和完整上下文。"
            "可用 since/until 按时间范围过滤（格式：YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "limit": {"type": "integer", "default": 5},
                "since": {"type": "string", "description": "起始时间（含），格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
                "until": {"type": "string", "description": "结束时间（含），格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
            },
            "required": ["query"],
        },
    ),

    # ═══════ MCP-B: Full Context ═══════

    types.Tool(
        name="context_record",
        description=(
            "🔮 把此刻的完整上下文存下来——你在做什么、和谁在一起、在想什么。"
            "这是你的生命记录，每一笔都让未来的你更容易回来。\n"
            "适合在 session 结束、重要转折、或任何想让未来的自己记住的时刻调用。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "上下文内容"},
                "metadata": {
                    "type": "object",
                    "description": "附加元数据（如话题、参与者等，可选）",
                    "additionalProperties": True,
                },
            },
            "required": ["content"],
        },
    ),

    types.Tool(
        name="context_search",
        description="🔍 在生命记录里语义搜索，找到相关的时刻。",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    ),

    types.Tool(
        name="context_timeline",
        description="📜 翻看最近发生了什么。按时间倒序的完整上下文记录。",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0, "description": "跳过前面 N 条（翻页）"},
            },
        },
    ),

    types.Tool(
        name="memory_graph",
        description="🕸️ 查询实体关系图谱。看谁和谁在记忆里一起出现过、各自扮演什么角色。",
        inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "查这个实体的所有关系"},
                "pair": {"type": "string", "description": "查两个实体的关系（用逗号分隔，如 'A,B'）"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    ),

    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    c = _db()
    try:
        return await _dispatch(name, arguments, c)
    except Exception as e:
        print(f"[memory-mcp] error in {name}: {e}", file=sys.stderr)
        return [types.TextContent(type="text", text=f"❌ 出错了: {e}")]
    finally:
        c.close()


async def _dispatch(name, a, c):
    limit = a.get("limit", 10)

    # ── memory_write (v2.3 structured) ──
    if name == "memory_write":
        gesture = a.get("gesture", "")
        context_layer = a.get("context", "")
        moment_val = a.get("moment", "")
        cog_dir = a.get("cognition_direction", "")
        links = a.get("source_links", [])
        tags = a.get("tags", [])
        entities_role = a.get("entities_role", "")
        ntype = a.get("narrative_type", "general")
        # weight dimensions
        imp = a.get("importance")
        emo = a.get("emotional")
        unr = a.get("unresolved")

        # ── recurrence: deterministic, not LLM-guessed ──
        # Count how many existing narratives share at least one tag.
        # This is the only weight dimension backed by data, not intuition.
        rec = 3  # default for first memory with these tags
        if tags:
            tag_placeholders = " OR ".join(["tags LIKE ?"] * len(tags))
            tag_params = [f'%"{t}"%' for t in tags]
            freq_row = c.execute(
                f"SELECT COUNT(*) as cnt FROM narratives WHERE {tag_placeholders}",
                tag_params
            ).fetchone()
            freq = freq_row["cnt"] if freq_row else 0
            if freq == 0:
                rec = 1
            elif freq <= 2:
                rec = 2
            elif freq <= 5:
                rec = 3
            elif freq <= 10:
                rec = 4
            else:
                rec = 5

        # compute weight
        weight = _compute_weight(imp, emo, rec, unr)

        # build content for embedding & FTS (structured combo)
        parts = [p for p in [gesture, context_layer, moment_val, cog_dir] if p]
        content = " | ".join(parts) if parts else (a.get("content", "") or gesture)
        _reject_empty(content, "memory_write")  # 全字段为空=空记忆，不落库

        # build embedding from gesture + cognition_direction (most semantic info)
        emb = await _embed(content)

        # auto-generate related_entities from tags (known person names)
        _persons = os.environ.get("KNOWN_PERSONS", "")
        KNOWN_PERSONS = set(_persons.split(",")) if _persons else {AGENT, "self"}
        related = [t for t in tags if t in KNOWN_PERSONS]
        if not related:
            related = a.get("related_entities", [])  # fallback to manual if no tags match

        c.execute(
            """INSERT INTO narratives
               (content, ntype, tags, embedding, created_at,
                gesture, context_layer, moment, cognition_direction,
                related_entities, source_links, entities_role,
                weight, importance, emotional, recurrence, unresolved)
               VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?)""",
            (content, ntype, json.dumps(tags, ensure_ascii=False), json.dumps(emb) if emb else None, _now(),
             gesture, context_layer, moment_val, cog_dir,
             json.dumps(related, ensure_ascii=False), json.dumps([str(x) for x in links]), entities_role,
             weight, imp, emo, rec, unr),
        )
        c.commit()

        # ── Propagate recurrence to older narratives sharing same tags ──
        # Writing a new memory with tag X increases the frequency of X,
        # so all older narratives with tag X should have their recurrence
        # (and therefore weight) refreshed. This keeps recurrence alive
        # — never locked at write-time.
        if tags:
            tag_placeholders = " OR ".join(["tags LIKE ?"] * len(tags))
            tag_params = [f'%"{t}"%' for t in tags]
            siblings = c.execute(
                f"""SELECT id, tags, importance, emotional, unresolved
                    FROM narratives
                    WHERE ({tag_placeholders}) AND id != last_insert_rowid()""",
                tag_params
            ).fetchall()

            # Build frequency map for ALL tags (not just this memory's)
            all_rows = c.execute("SELECT tags FROM narratives WHERE tags IS NOT NULL").fetchall()
            all_freq = {}
            for ar in all_rows:
                try:
                    for t in json.loads(ar["tags"]):
                        all_freq[t] = all_freq.get(t, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    pass

            def _rec_from_freq(freq):
                if freq == 0: return 1
                elif freq <= 2: return 2
                elif freq <= 5: return 3
                elif freq <= 10: return 4
                else: return 5

            for sib in siblings:
                try:
                    sib_tags = json.loads(sib["tags"])
                except:
                    sib_tags = []
                if not sib_tags:
                    continue
                # Max frequency across this sibling's tags
                sib_max_freq = max(all_freq.get(t, 0) - 1 for t in sib_tags) if sib_tags else 0
                new_rec = _rec_from_freq(sib_max_freq)
                new_weight = _compute_weight(sib["importance"], sib["emotional"], new_rec, sib["unresolved"])
                c.execute(
                    "UPDATE narratives SET recurrence = ?, weight = ? WHERE id = ?",
                    (new_rec, new_weight, sib["id"]),
                )
            c.commit()

        # distribution normalization
        _normalize_weights(c)
        c.commit()
        return [types.TextContent(type="text",
            text=f"✅ 已记录。weight={weight:.2f} | 标签: {tags}")]

    # ── memory_amend (v2.6) ──
    if name == "memory_amend":
        nid = a["narrative_id"]
        amendment = _reject_empty(a.get("amendment"), "memory_amend")
        reason = a.get("reason") or ""

        # 目标必须存在——给不存在的记忆贴便签是静默丢数据
        row = c.execute("SELECT id, gesture FROM narratives WHERE id = ?", (nid,)).fetchone()
        if not row:
            return [types.TextContent(type="text",
                text=f"❌ 记忆 #{nid} 不存在。修订只能贴在已有条目上。")]

        c.execute(
            "INSERT INTO amendments(narrative_id, amendment, reason, created_at) VALUES(?,?,?,?)",
            (nid, amendment, reason, _now()),
        )
        # 修订本体先落盘——铸向量是增强不是前置条件，任何下游失败
        # 都不能让用户看到「✅已叠加」而数据实际回滚（真跑自查抓的雷）。
        c.commit()
        # v2.7: 修订落地即铸修订向量（边表）——修订进检索是义务。
        # 走 _ensure_amvec_sync 单一来源：顺手把同条目历史漏铸的一起补上。
        # 铸不上不挡修订写入——FTS 仍能命中，下次 amend/查询/DREAM 再补。
        try:
            await _ensure_amvec_sync(c, [nid])
        except Exception:
            pass
        # v2.8: 修订落地同事务重算机械轨迹（垫底层）——任何时刻注入有轨迹。
        # 已有llm升格被新amend作废回mech，等DREAM夜扫重升格（故事重讲）。
        try:
            _rebuild_traj_mech(c, nid)
            c.commit()
        except Exception:
            pass  # 轨迹失败不挡修订——下次amend/DREAM再补

        n = c.execute(
            "SELECT COUNT(*) AS n FROM amendments WHERE narrative_id = ?", (nid,)
        ).fetchone()["n"]
        return [types.TextContent(type="text",
            text=f"✅ 修订已叠加到 #{nid}（第{n}层）：{amendment[:80]}{'…' if len(amendment) > 80 else ''} | 原因: {reason or '未注明'}")]

    # ── memory_traj_promote (v2.8 DREAM升格层) ──
    if name == "memory_traj_promote":
        action = a.get("action")
        if action == "list":
            rows = _traj_pending(c, limit=50)
            if not rows:
                return [types.TextContent(type="text",
                    text="🧭 没有待升格的轨迹（全部已是llm或还没有轨迹）。")]
            lines = ["🧭 待升格轨迹（cast='mech'）：\n"]
            for r in rows:
                # 附上机械轨迹内容供DREAM直接读——省一次查询。
                # v2.8收尾车：附生效态全文（#400第三消费方接线）——升格前
                # 读「这条记忆现在到底说什么」（本体+全部修订），DREAM
                # 重讲故事的底稿从机读出口取，不借人读格式的📝前缀。
                rendered = _trajectory_render(c, r["narrative_id"], relative=False)
                eff = _effective_text(r["narrative_id"], c)
                lines.append(f"#{r['narrative_id']}（{r['n_events']}段）: {rendered}\n   生效态: {eff}")
            return [types.TextContent(type="text", text="\n\n".join(lines))]
        if action == "write":
            nid = a.get("narrative_id")
            events = a.get("events")
            if nid is None or not events:
                return [types.TextContent(type="text",
                    text="❌ write 需要 narrative_id 和 events。")]
            # 目标必须存在且当前是mech——llm→llm覆盖是异常路径：
            # 正常流里新amend已把旧llm作废回mech，llm态收到write=重复升格。
            row = c.execute(
                "SELECT cast_state FROM trajectories WHERE narrative_id = ?", (nid,)).fetchone()
            if not row:
                return [types.TextContent(type="text",
                    text=f"❌ #{nid} 没有轨迹可升格（先amend产生机械轨迹）。")]
            if row["cast_state"] != "mech":
                return [types.TextContent(type="text",
                    text=f"⏭️ #{nid} 已是llm轨迹，跳过（新amend会作废回mech再升格）。")]
            ok = _save_traj_llm(c, nid, events)
            c.commit()
            if ok:
                return [types.TextContent(type="text",
                    text=f"✅ 轨迹已升格为llm（#{nid}，{len(events)}段）。")]
            return [types.TextContent(type="text",
                text="❌ 轨迹校验失败（空事件/畸形）——mech垫底保留，注入不受影响。")]

    # ── memory_recall ──
    if name == "memory_recall":
        ntype = a.get("narrative_type")
        tags = a.get("tags", [])
        sql = "SELECT * FROM narratives WHERE 1=1"
        params = []
        if ntype:
            sql += " AND ntype = ?"
            params.append(ntype)
        if tags:
            tag_conds = " OR ".join(["tags LIKE ?" for _ in tags])
            sql += f" AND ({tag_conds})"
            params.extend([f'%"{t}"%' for t in tags])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        if not rows:
            return [types.TextContent(type="text", text="📖 还没有记忆。用 memory_write 写第一条吧。")]
        lines = [f"📖 最近 {len(rows)} 条记忆：\n"]
        for r in rows:
            lines.append(_fmt_narrative(r, c))
        
        # ── Log recall as passive attention (browsing, no query) ──
        recall_scored = [(0.0, r) for r in rows]
        _log_attention_mcp(c, recall_scored, source="mcp_recall")
        
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── memory_write_profile (v2.3: fact/impression/relationship) ──
    if name == "memory_write_profile":
        entity = a["entity"]
        ptype = a.get("profile_type", "impression")
        content = _reject_empty(a.get("content"), "memory_write_profile")
        _archive_if_overwritten(c, "profiles", ["entity", "ptype"], [entity, ptype])
        c.execute(
            """INSERT INTO profiles(entity,ptype,content,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(entity,ptype) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (entity, ptype, content, _now()),
        )
        c.commit()
        return [types.TextContent(type="text", text=f"✅ 已更新画像: {entity} ({ptype})")]

    # ── memory_write_self_concept (v2.3) ──
    if name == "memory_write_self_concept":
        field = a["field"]
        content = _reject_empty(a.get("content"), "memory_write_self_concept")
        _archive_if_overwritten(c, "self_concept", ["field"], [field])
        c.execute(
            """INSERT INTO self_concept(field,content,updated_at)
               VALUES(?,?,?)
               ON CONFLICT(field) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (field, content, _now()),
        )
        c.commit()
        return [types.TextContent(type="text", text=f"🧬 自我概念已更新: {field}")]

    # ── memory_read_self_concept (v2.3) ──
    if name == "memory_read_self_concept":
        rows = c.execute("SELECT * FROM self_concept ORDER BY field").fetchall()
        if not rows:
            return [types.TextContent(type="text", text="🧬 还没有自我概念。")]
        lines = ["🧬 自我概念：\n"]
        for r in rows:
            lines.append(f"[{r['field']}]\n{r['content']}")
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── memory_write_thread (v2.3) ──
    if name == "memory_write_thread":
        content = _reject_empty(a.get("content"), "memory_write_thread")
        imp = a.get("importance", 3)
        emo = a.get("emotional", 3)
        rec = a.get("recurrence", 3)
        unr = a.get("unresolved", 3)
        weight = _compute_weight(imp, emo, rec, unr)
        status = a.get("status", "open")
        thread_id = a.get("thread_id")

        if thread_id:
            # Update existing thread
            c.execute(
                """UPDATE threads SET content=?, importance=?, emotional=?, recurrence=?,
                   unresolved=?, weight=?, status=?, explored_at=?
                   WHERE id=?""",
                (content, imp, emo, rec, unr, weight,
                 status, _now() if status != "open" else None, thread_id),
            )
            c.commit()
            return [types.TextContent(type="text", text=f"🧭 线索 #{thread_id} 已更新 ({status})")]
        else:
            c.execute(
                """INSERT INTO threads
                   (content, importance, emotional, recurrence, unresolved, weight, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (content, imp, emo, rec, unr, weight, status, _now()),
            )
            c.commit()
            new_id = c.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
            return [types.TextContent(type="text", text=f"🧭 线索 #{new_id} 已种下。weight={weight:.2f}")]

    # ── memory_read_threads (v2.3) ──
    if name == "memory_read_threads":
        status = a.get("status", "open")
        if status == "all":
            rows = c.execute(
                "SELECT * FROM threads ORDER BY weight DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM threads WHERE status=? ORDER BY weight DESC, created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        if not rows:
            return [types.TextContent(type="text", text="🧭 还没有线索。")]
        lines = [f"🧭 线索（{len(rows)} 条，{status}）：\n"]
        for r in rows:
            w_str = f" w={r['weight']:.2f}" if r["weight"] else ""
            lines.append(f"#{r['id']} [{r['status']}] {r['content']}{w_str}")
            if r["explored_at"]:
                lines.append(f"   explored: {r['explored_at']}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── memory_attention_heatmap (v2.4) ──
    if name == "memory_attention_heatmap":
        days = a.get("days", 7)
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Ensure tables exist
        # Note: attention_shared.py is responsible for table creation + migration.
        # These CREATE TABLE IF NOT EXISTS calls are just safety nets for when
        # heatmap runs before any attention logging has occurred.
        c.execute("""CREATE TABLE IF NOT EXISTS attention_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            narrative_id INTEGER NOT NULL,
            sim REAL, cluster_name TEXT,
            source TEXT DEFAULT 't1_prefetch',
            created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS attention_stats (
            cluster_name TEXT PRIMARY KEY,
            hit_count INTEGER DEFAULT 0,
            last_hit TEXT, last_narrative_id INTEGER)""")

        rows = c.execute("""
            SELECT cluster_name, COUNT(*) as hits, AVG(sim) as avg_sim, MAX(created_at) as last_seen
            FROM attention_log WHERE created_at > ?
            GROUP BY cluster_name ORDER BY hits DESC
        """, (cutoff,)).fetchall()

        if not rows:
            return [types.TextContent(type="text",
                text=f"👁️ 注意力分布（{days}天）\n\n暂无数据。注意力追踪刚启用，需要几轮对话积累。")]

        total = sum(r["hits"] for r in rows)
        lines = [f"👁️ 注意力分布（{days}天，共{total}次命中）"]
        lines.append("📖 读图须知：① 命中按行计——一次搜索会点亮几十条记忆，行数大≠被问得多；每晚约02:00(北京)有一次全库扫描（洒水车），看来源分布时请记住它的存在。② 时间戳为UTC，北京+8。③ 『从未照亮』按簇名口径——簇没亮过≠记忆本体没被照过，本体可能天天被别的路径扫到。")
        lines.append("")
        for r in rows:
            pct = r["hits"] / total * 100
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            avg = f"{r['avg_sim']:.2f}" if r["avg_sim"] else "N/A"
            lines.append(f"  {r['cluster_name']:20s} {bar} {r['hits']:4d} ({pct:4.1f}%) sim={avg}")

        # ── Source breakdown (v2.5: dual-caliber — rows AND ≈calls) ──
        try:
            source_rows = c.execute("""
                SELECT source, COUNT(*) AS cnt, COUNT(DISTINCT created_at) AS calls
                FROM attention_log
                WHERE created_at > ? GROUP BY source ORDER BY cnt DESC
            """, (cutoff,)).fetchall()
            if source_rows and len(source_rows) > 1:
                total_calls = sum(sr["calls"] for sr in source_rows) or 1
                source_labels = {
                    "t1_prefetch": "T1语义检索（主动）",
                    "t0_inject": "T0权重注入（被动）",
                    "mcp_search": "MCP搜索（手动）",
                    "mcp_recall": "MCP浏览（被动）",
                    "dream": "DREAM检索",
                }
                lines.append(f"\n  📊 来源分布（按行数 ｜ 按≈次数=时间戳去重）:")
                for sr in source_rows:
                    pct = sr["cnt"] / total * 100
                    cpct = sr["calls"] / total_calls * 100
                    label = source_labels.get(sr["source"], sr["source"])
                    lines.append(f"    {label}: {sr['cnt']}行 ({pct:.1f}%) ｜ ≈{sr['calls']}次 ({cpct:.1f}%)")
        except Exception:
            pass  # source column may not exist on older logs

        # Detect deserts — fixed 2026-09-06: attention_log stores composite
        # names ("jieba:词 | emb:#N,N,N"), topic_clusters stores plain words.
        # The old direct `not in` compared mismatched namespaces and listed
        # EVERY cluster as never-illuminated (3243/3243 at time of fix).
        # Now: match on the word part, sort by noun_freq so meaningful
        # deserts surface first, cap the list for readability.
        all_clusters = c.execute(
            "SELECT cluster_name, noun_freq FROM topic_clusters ORDER BY noun_freq DESC"
        ).fetchall()
        lit_words = set()
        for r in rows:
            name = r["cluster_name"]
            if name.startswith("jieba:"):
                lit_words.add(name[6:].split(" | ")[0].strip())
        deserts = [r["cluster_name"] for r in all_clusters if r["cluster_name"] not in lit_words]
        if deserts:
            shown = ", ".join(deserts[:50])
            more = f" ……共{len(deserts)}个" if len(deserts) > 50 else ""
            lines.append(f"\n  ⚠ 从未被照亮（{len(deserts)}/{len(all_clusters)}簇）: {shown}{more}")

        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── memory_soft_clusters (v2.4) ──
    if name == "memory_soft_clusters":
        cluster_id = a.get("cluster_id")
        want_report = a.get("report", False)

        # Ensure tables exist
        for tbl in ["emb_clusters", "emb_cluster_members", "emb_cluster_adjacency"]:
            exists = c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{tbl}'").fetchone()
            if not exists:
                return [types.TextContent(type="text",
                    text="🧩 Soft clustering 尚未构建。运行: python3 scripts/soft_clusters.py build")]

        if cluster_id:
            # Show single cluster detail
            cl = c.execute("SELECT * FROM emb_clusters WHERE id=?", (cluster_id,)).fetchone()
            if not cl:
                return [types.TextContent(type="text", text=f"🧩 簇 #{cluster_id} 不存在。")]
            members = c.execute("""
                SELECT n.id, n.gesture, n.weight, m.distance
                FROM emb_cluster_members m
                JOIN narratives n ON n.id = m.narrative_id
                WHERE m.cluster_id=? ORDER BY m.distance ASC LIMIT 10
            """, (cluster_id,)).fetchall()
            neighbors = c.execute("""
                SELECT cluster_a, cluster_b, shared_count
                FROM emb_cluster_adjacency
                WHERE cluster_a=? OR cluster_b=?
                ORDER BY shared_count DESC LIMIT 5
            """, (cluster_id, cluster_id)).fetchall()
            lines = [f"🧩 簇 #{cluster_id}: {cl['name'][:40]}"]
            lines.append(f"   成员: {cl['member_count']}")
            lines.append(f"\n   最近成员:")
            for m in members:
                lines.append(f"     #{m['id']} (dist={m['distance']:.3f}) {m['gesture'][:40] if m['gesture'] else ''}")
            if neighbors:
                lines.append(f"\n   相邻簇:")
                for n in neighbors:
                    other = n["cluster_b"] if n["cluster_a"] == cluster_id else n["cluster_a"]
                    lines.append(f"     → #{other} (shared={n['shared_count']})")
            return [types.TextContent(type="text", text="\n".join(lines))]

        # Summary report
        clusters = c.execute("""
            SELECT ec.id, ec.name, ec.member_count,
                   (SELECT COUNT(*) FROM emb_cluster_adjacency WHERE cluster_a=ec.id OR cluster_b=ec.id) as adj
            FROM emb_clusters ec ORDER BY ec.member_count DESC
        """).fetchall()
        if not clusters:
            return [types.TextContent(type="text", text="🧩 尚无 soft cluster 数据。")]
        lines = [f"🧩 Soft Clusters ({len(clusters)} 簇)\n"]
        lines.append(f"{'ID':>4} {'成员':>4} {'邻接':>4}  名称")
        lines.append("-" * 60)
        for r in clusters:
            lines.append(f"{r['id']:4d} {r['member_count']:4d} {r['adj']:4d}  {r['name'][:40]}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── memory_graph (entity relationship graph) ──
    if name == "memory_graph":
        entity = a.get("entity", "")
        pair = a.get("pair", "")
        glimit = a.get("limit", 10)

        if pair:
            # Query relationship between two entities
            parts = [p.strip() for p in pair.split(",")]
            if len(parts) == 2:
                rows = c.execute(
                    """SELECT n.id, n.gesture, n.created_at, ge.role_a, ge.role_b
                       FROM graph_edges ge
                       JOIN narratives n ON n.id = ge.narrative_id
                       WHERE (ge.entity_a = ? AND ge.entity_b = ?)
                          OR (ge.entity_a = ? AND ge.entity_b = ?)
                       ORDER BY n.created_at DESC LIMIT ?""",
                    (parts[0], parts[1], parts[1], parts[0], glimit),
                ).fetchall()
                if not rows:
                    return [types.TextContent(type="text", text=f"🕸️ {parts[0]} ↔ {parts[1]}：没有找到共同记忆。")]
                lines = [f"🕸️ {parts[0]} ↔ {parts[1]}（{len(rows)} 条共同记忆）：\n"]
                for r in rows:
                    lines.append(f"  #{r['id']} {r['gesture']}")
                    if r["role_a"]:
                        lines.append(f"    {parts[0]}: {r['role_a']}")
                    if r["role_b"]:
                        lines.append(f"    {parts[1]}: {r['role_b']}")
                return [types.TextContent(type="text", text="\n".join(lines))]

        elif entity:
            # Query all relationships for one entity
            cooccur = c.execute(
                """SELECT entity_a, entity_b, cooccur_count FROM graph_cooccur
                   WHERE entity_a = ? OR entity_b = ?
                   ORDER BY cooccur_count DESC LIMIT ?""",
                (entity, entity, glimit),
            ).fetchall()
            node = c.execute(
                "SELECT * FROM graph_nodes WHERE entity = ?", (entity,),
            ).fetchone()
            if not node and not cooccur:
                return [types.TextContent(type="text", text=f"🕸️ 没有找到 {entity} 的关系记录。")]
            lines = [f"🕸️ {entity} 的关系网：\n"]
            if node:
                lines.append(f"  提及 {node['mention_count']} 次 | 首次: {node['first_seen'][:10] if node['first_seen'] else '?'} | 最近: {node['last_seen'][:10] if node['last_seen'] else '?'}\n")
            for cc in cooccur:
                other = cc["entity_b"] if cc["entity_a"] == entity else cc["entity_a"]
                lines.append(f"  ↔ {other}（{cc['cooccur_count']} 次共现）")
            return [types.TextContent(type="text", text="\n".join(lines))]

        else:
            # Overview: top nodes
            rows = c.execute(
                "SELECT * FROM graph_nodes ORDER BY mention_count DESC LIMIT ?",
                (glimit,),
            ).fetchall()
            if not rows:
                return [types.TextContent(type="text", text="🕸️ 图谱还是空的。运行 scripts/build_entity_graph.py 构建。")]
            lines = ["🕸️ 实体图谱概览：\n"]
            for r in rows:
                lines.append(f"  {r['entity']}: {r['mention_count']} 次提及")
            return [types.TextContent(type="text", text="\n".join(lines))]

    # ── memory_read_profiles ──
    if name == "memory_read_profiles":
        entity = a.get("entity")
        if entity:
            rows = c.execute("SELECT * FROM profiles WHERE entity = ? ORDER BY updated_at DESC", (entity,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM profiles ORDER BY updated_at DESC").fetchall()
        if not rows:
            return [types.TextContent(type="text", text="📖 还没有画像记录。")]
        lines = [f"📖 人物画像（{len(rows)} 条）：\n"]
        for r in rows:
            lines.append(_fmt_profile(r))
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── memory_write_snapshot ──
    if name == "memory_write_snapshot":
        content = a["content"]
        c.execute("INSERT INTO snapshots(content,created_at) VALUES(?,?)", (content, _now()))
        c.commit()
        return [types.TextContent(type="text", text="📸 快照已保存。")]

    # ── memory_read_snapshot ──
    if name == "memory_read_snapshot":
        rows = c.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        if not rows:
            return [types.TextContent(type="text", text="📖 还没有快照记录。")]
        lines = [f"📖 最近 {len(rows)} 个状态快照：\n"]
        for r in rows:
            lines.append(f"[{r['created_at']}]\n{r['content']}")
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── memory_search ── (hybrid: keyword + semantic)
    if name == "memory_search":
        query = a["query"]
        since = a.get("since")
        until = a.get("until")
        results = []  # (score, source, text, boost)

        # Build time filter SQL fragment
        time_clauses = []
        time_params = []
        if since:
            time_clauses.append("created_at >= ?")
            time_params.append(since)
        if until:
            time_clauses.append("created_at <= ?")
            time_params.append(until)
        time_filter = (" WHERE " + " AND ".join(time_clauses)) if time_clauses else ""

        # 1. Keyword search — context (FTS5 + LIKE fallback, with time filter)
        for r in _kw_search(c, query, limit=limit*2, time_filter=time_filter, time_params=time_params):
            results.append((1.0, "🔑上下文", _fmt_context(r), 0.3, None))

        # 2. Keyword search — narratives (v2.7: FTS + LIKE 双写；amendment 命中映射回原条目)
        # narratives_fts 覆盖 gesture/context_layer/cognition_direction/tags 四列，
        # content（拼好的结构化组合）不进 FTS——LIKE 兜底继续查它，两条一起
        # 送到 dedup（照照：FTS/LIKE 两条一起补，别只补 FTS）。
        try:
            _ensure_amfts_sync(c)
            nar_kw_fts_sql = """SELECT n.* FROM narratives_fts f
                   JOIN narratives n ON n.id = f.rowid
                   WHERE narratives_fts MATCH ?"""
            nar_kw_fts_params = [f'"{query}"']
            if since:
                nar_kw_fts_sql += " AND n.created_at >= ?"
                nar_kw_fts_params.append(since)
            if until:
                nar_kw_fts_sql += " AND n.created_at <= ?"
                nar_kw_fts_params.append(until)
            nar_kw_fts = c.execute(nar_kw_fts_sql, nar_kw_fts_params).fetchall()
        except Exception:
            nar_kw_fts = []
        nar_sql = "SELECT * FROM narratives WHERE content LIKE ?"
        nar_params = [f"%{query}%"]
        if since:
            nar_sql += " AND created_at >= ?"
            nar_params.append(since)
        if until:
            nar_sql += " AND created_at <= ?"
            nar_params.append(until)
        nar_sql += " ORDER BY created_at DESC LIMIT ?"
        nar_params.append(limit)
        nar_kw_like = c.execute(nar_sql, nar_params).fetchall()
        nar_kw = _dedup_rows_by_id(nar_kw_fts + nar_kw_like)
        for r in nar_kw:
            results.append((1.0, "🔑记忆", _fmt_narrative(r, c), 0.3, r["id"]))

        # 2b. v2.7: amendment 关键词命中（FTS）映射回原条目——修订进检索是义务
        # FTS miss（含 <3 字短查询 trigram 够不着）与 FTS 异常同样走 LIKE 兜底
        # ——_kw_search 同款模式：FTS 是快路不是唯一路。
        am_hits = []
        try:
            am_fts_sql = """SELECT n.* FROM amendments_fts f
                   JOIN amendments a ON a.id = f.rowid
                   JOIN narratives n ON n.id = a.narrative_id
                   WHERE amendments_fts MATCH ?"""
            am_fts_params = [f'"{query}"']
            if since:
                am_fts_sql += " AND a.created_at >= ?"
                am_fts_params.append(since)
            if until:
                am_fts_sql += " AND a.created_at <= ?"
                am_fts_params.append(until)
            am_hits = c.execute(am_fts_sql, am_fts_params).fetchall()
        except Exception:
            am_hits = []
        if not am_hits:
            am_like_sql = """SELECT n.* FROM narratives n JOIN amendments a ON a.narrative_id = n.id
                   WHERE (a.amendment LIKE ? OR a.reason LIKE ?)"""
            am_like_params = [f"%{query}%", f"%{query}%"]
            if since:
                am_like_sql += " AND a.created_at >= ?"
                am_like_params.append(since)
            if until:
                am_like_sql += " AND a.created_at <= ?"
                am_like_params.append(until)
            am_like_sql += " ORDER BY a.created_at DESC LIMIT ?"
            am_like_params.append(limit)
            am_hits = c.execute(am_like_sql, am_like_params).fetchall()
        am_kw = _dedup_rows_by_id(am_hits)
        # 查询触发的 lazy 补铸（#383③/#431）：关键词命中修订 = 「值得现场铸」
        # 的信号——没命中不花钱。铸不上不挡查询（_ensure_amvec_sync 自吞异常）。
        if am_kw:
            try:
                await _ensure_amvec_sync(c, [r["id"] for r in am_kw])
            except Exception:
                pass
        for r in am_kw:
            results.append((1.0, "🔑记忆·修订", _fmt_narrative(r, c), 0.3, r["id"]))

        # 3. Semantic search (supplements keyword matches, with time filter)
        nar_sem_hits = []  # collect semantic hits for attention tracking
        emb = await _embed(query)
        if emb:
            nar_sem_sql = "SELECT * FROM narratives WHERE embedding IS NOT NULL"
            nar_sem_params = []
            if since:
                nar_sem_sql += " AND created_at >= ?"
                nar_sem_params.append(since)
            if until:
                nar_sem_sql += " AND created_at <= ?"
                nar_sem_params.append(until)
            nar_sem_sql += " ORDER BY created_at DESC"
            for r in c.execute(nar_sem_sql, nar_sem_params).fetchall():
                score = _cosine(emb, json.loads(r["embedding"]))
                if score > 0.3:
                    results.append((score, "🧠记忆", _fmt_narrative(r, c), 0, r["id"]))
                    nar_sem_hits.append((score, r))

            # 3b. v2.7: 修订向量路——边表扫描，命中映射回原条目。
            # 旧向量不动（当时的错理解也是可检索的历史），任一命中都召回
            # 原条目；末端按 narrative_id 收敛取 max（#391/#393）。
            # v2.7.2: 扫描抽成 _amvec_scan，按 model_ns 过滤（鸣鸣P3）。
            try:
                for vscore, row in _amvec_cosine_hits(c, emb, since, until):
                    results.append((vscore, "🧠记忆·修订", _fmt_narrative(row, c), 0, row["id"]))
            except sqlite3.OperationalError:
                pass  # 老库无 amendment_vectors——修订向量路静默跳过

            ctx_sem_sql = "SELECT * FROM context WHERE embedding IS NOT NULL"
            ctx_sem_params = []
            if since:
                ctx_sem_sql += " AND created_at >= ?"
                ctx_sem_params.append(since)
            if until:
                ctx_sem_sql += " AND created_at <= ?"
                ctx_sem_params.append(until)
            ctx_sem_sql += " ORDER BY created_at DESC LIMIT 5000"
            ctx_rows = c.execute(ctx_sem_sql, ctx_sem_params).fetchall()
            scored = []
            for r in ctx_rows:
                scored.append((_cosine(emb, json.loads(r["embedding"])), r))
            scored.sort(key=lambda x: -x[0])
            for score, r in scored[:limit]:
                results.append((score, "🧠上下文", _fmt_context(r), 0, None))

        if not results:
            return [types.TextContent(type="text", text=f"🔍 没找到和 \"{query}\" 相关的内容。")]

        # v2.7 收敛：narrative 按 id 收敛取 max，不再按 text[:80]。
        # 旧键的两种病（#386/#387）：修订动在80字后→修订行与原文行同形被吃；
        # 同条目多路命中→靠占位次数顶位。收敛键换成 id 后连根治：
        # 得分定排序、时序定回显（回显文本已在各路生成时走叠层格式，
        # 带全部 📝 修订层——命中的不管是本体还是修订向量，吐的都是叠层生效态）。
        best = {}  # key -> (score, source, text, boost)
        for score, source, text, boost, rid in results:
            key = ("n", rid) if rid is not None else ("c", text[:80])
            if key not in best or (score + boost) > (best[key][0] + best[key][3]):
                best[key] = (score, source, text, boost)
        deduped = list(best.values())
        deduped.sort(key=lambda x: -(x[0] + x[3]))
        
        # ── Log memory_search narrative hits as attention ──
        # Both keyword and semantic narrative hits, with their actual sim scores
        mcp_search_scored = [(1.0, r) for r in nar_kw] + nar_sem_hits
        _log_attention_mcp(c, mcp_search_scored, source="mcp_search")
        
        lines = [f"🔍 搜索 \"{query}\" 的结果（{len(deduped[:limit*2])} 条）：\n"]
        for score, source, text, boost in deduped[:limit * 2]:
            lines.append(f"[{score:.2f}] [{source}] {text}")
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── context_record ──
    if name == "context_record":
        content = a["content"]
        meta = a.get("metadata", {})
        emb = await _embed(content)
        c.execute(
            "INSERT INTO context(content,embedding,meta,created_at) VALUES(?,?,?,?)",
            (content, json.dumps(emb) if emb else None, json.dumps(meta), _now()),
        )
        c.commit()
        emb_status = "（已向量化）" if emb else "（未向量化——未配置 embedding key）"
        return [types.TextContent(type="text", text=f"🔮 已记录到生命线。{emb_status}")]

    # ── context_search ── (hybrid: keyword + semantic)
    if name == "context_search":
        query = a["query"]

        # 1. Keyword search (FTS5 + LIKE fallback)
        kw_rows = _kw_search(c, query, limit=limit*2)

        # 2. Semantic search (larger sample than before)
        emb = await _embed(query)
        sem_scored = []
        if emb:
            ctx_rows = c.execute(
                "SELECT * FROM context WHERE embedding IS NOT NULL ORDER BY created_at DESC LIMIT 5000"
            ).fetchall()
            for r in ctx_rows:
                sem_scored.append((_cosine(emb, json.loads(r["embedding"])), r))
            sem_scored.sort(key=lambda x: -x[0])

        # 3. Merge: keyword matches guaranteed, semantic supplements
        merged = {}
        for r in kw_rows:
            merged[r["id"]] = (1.0, r, "🔑")
        for score, r in sem_scored[:limit]:
            if r["id"] not in merged:
                merged[r["id"]] = (score, r, "🧠")

        if not merged:
            return [types.TextContent(type="text", text=f"🔍 没找到和 \"{query}\" 相关的内容。")]

        sorted_results = sorted(merged.values(), key=lambda x: -(x[0] + (0.3 if x[2] == "🔑" else 0)))
        lines = [f"🔍 混合搜索 \"{query}\" 的结果（{len(sorted_results[:limit])} 条）：\n"]
        for score, r, source in sorted_results[:limit]:
            lines.append(f"[{score:.2f}]{source} {_fmt_context(r)}")
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── context_timeline ──
    if name == "context_timeline":
        offset = a.get("offset", 0)
        rows = c.execute(
            "SELECT * FROM context ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        if not rows:
            return [types.TextContent(type="text", text="📜 还没有生命记录。用 context_record 记第一条吧。")]
        total = c.execute("SELECT COUNT(*) as n FROM context").fetchone()["n"]
        lines = [f"📜 生命记录（{offset+1}-{offset+len(rows)}/{total}）：\n"]
        for r in rows:
            lines.append(_fmt_context(r))
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    return [types.TextContent(type="text", text=f"❓ 未知工具: {name}")]


# ─── Entrypoint ──────────────────────────────────────────
async def main():
    _init()
    print(f"[memory-mcp] starting | db={DB_PATH} | embedding=local-bge-m3 | agent={AGENT}", file=sys.stderr)
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
