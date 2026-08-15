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

import os, sys, json, math, sqlite3
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
    """)
    c.commit(); c.close()

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

async def _embed(text: str, _retries: int = 3):
    """Return embedding vector via local bge-m3 or remote OpenAI-compatible API.

    Includes retry logic — embedding server may be briefly unavailable.
    Logs to stderr on each failure so silent drops are visible.
    """
    import httpx, asyncio as _aio
    last_err = None
    for attempt in range(_retries):
        try:
            headers = {}
            if _is_local_emb():
                payload = {"texts": [text[:5000]]}
                async with httpx.AsyncClient(trust_env=False, timeout=60) as cli:
                    r = await cli.post(_EMB_URL, json=payload)
                    r.raise_for_status()
                    return r.json()["embeddings"][0]
            else:
                headers["Authorization"] = f"Bearer {_EMB_KEY}"
                payload = {"model": _EMB_MODEL, "input": text[:5000]}
                async with httpx.AsyncClient(trust_env=False, timeout=60) as cli:
                    r = await cli.post(_EMB_URL, json=payload, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    return data["data"][0]["embedding"] if "data" in data else data["embeddings"][0]
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
def _fmt_narrative(r):
    """v2.3: structured display — gesture is the headline, rest is detail."""
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
            if ents: parts.append(f"   👤 {', '.join(ents)}")
        if links:
            lks = json.loads(links) if links else []
            if lks: parts.append(f"   🔗 {', '.join(str(x) for x in lks)}")
        w_str = f"  w={weight:.2f}" if weight else ""
        parts.append(f"   [{r['created_at']}]{w_str}")
        return "\n".join(parts)
    else:
        # Legacy free-text entry
        tags = json.loads(r["tags"]) if r["tags"] else []
        tag_str = f"  tags: {', '.join(tags)}" if tags else ""
        preview = r["content"][:300]
        if len(r["content"]) > 300:
            preview += "..."
        return f"[{r['created_at']}] [{r['ntype']}] {preview}{tag_str}"

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
        links = [str(x) for x in a.get("source_links", []) if str(x).strip()]
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
            (content, ntype, json.dumps(tags), json.dumps(emb) if emb else None, _now(),
             gesture, context_layer, moment_val, cog_dir,
             json.dumps(related), json.dumps(links), entities_role,
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
            lines.append(_fmt_narrative(r))
        
        # ── Log recall as passive attention (browsing, no query) ──
        recall_scored = [(0.0, r) for r in rows]
        _log_attention_mcp(c, recall_scored, source="mcp_recall")
        
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    # ── memory_write_profile (v2.3: fact/impression/relationship) ──
    if name == "memory_write_profile":
        entity = a["entity"]
        ptype = a.get("profile_type", "impression")
        content = a["content"]
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
        content = a["content"]
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
        content = a["content"]
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
        lines = [f"👁️ 注意力分布（{days}天，共{total}次命中）\n"]
        for r in rows:
            pct = r["hits"] / total * 100
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            avg = f"{r['avg_sim']:.2f}" if r["avg_sim"] else "N/A"
            lines.append(f"  {r['cluster_name']:20s} {bar} {r['hits']:4d} ({pct:4.1f}%) sim={avg}")

        # ── Source breakdown (v2.4: active vs passive attention) ──
        try:
            source_rows = c.execute("""
                SELECT source, COUNT(*) as cnt FROM attention_log
                WHERE created_at > ? GROUP BY source ORDER BY cnt DESC
            """, (cutoff,)).fetchall()
            if source_rows and len(source_rows) > 1:
                source_labels = {
                    "t1_prefetch": "T1语义检索（主动）",
                    "t0_inject": "T0权重注入（被动）",
                    "mcp_search": "MCP搜索（手动）",
                    "mcp_recall": "MCP浏览（被动）",
                    "dream": "DREAM检索",
                }
                lines.append(f"\n  📊 来源分布:")
                for sr in source_rows:
                    pct = sr["cnt"] / total * 100
                    label = source_labels.get(sr["source"], sr["source"])
                    lines.append(f"    {label}: {sr['cnt']} ({pct:.1f}%)")
        except Exception:
            pass  # source column may not exist on older logs

        # Detect deserts
        all_clusters = c.execute("SELECT cluster_name FROM topic_clusters").fetchall()
        lit = {r["cluster_name"] for r in rows}
        deserts = [r[0] for r in all_clusters if r[0] not in lit]
        if deserts:
            lines.append(f"\n  ⚠ 从未被照亮: {', '.join(deserts)}")

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
            results.append((1.0, "🔑上下文", _fmt_context(r), 0.3))

        # 2. Keyword search — narratives (LIKE, with time filter)
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
        nar_kw = c.execute(nar_sql, nar_params).fetchall()
        for r in nar_kw:
            results.append((1.0, "🔑记忆", _fmt_narrative(r), 0.3))

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
                    results.append((score, "🧠记忆", _fmt_narrative(r), 0))
                    nar_sem_hits.append((score, r))

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
                results.append((score, "🧠上下文", _fmt_context(r), 0))

        if not results:
            return [types.TextContent(type="text", text=f"🔍 没找到和 \"{query}\" 相关的内容。")]

        # Deduplicate by content preview, sort with keyword boost
        seen = set()
        deduped = []
        for score, source_text, text, boost in results:
            key = text[:80]
            if key not in seen:
                seen.add(key)
                deduped.append((score, source_text, text, boost))
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
