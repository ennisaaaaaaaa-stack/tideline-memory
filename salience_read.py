#!/usr/bin/env python3
"""salience_read — salience 读口：显著性过滤器问 Tideline 的那条只读检索通道。

spec: ~/drafts/salience-read-port-spec-v0.md（HN gate 三件套之一）

定位（一句话）：器官发来原始事件，过滤器要判定「这段输入此刻对这位 agent
重要吗」——判定所需的结构化背景（她在乎什么、谁重要、什么被 pin 住了）
全部从这条读口出。

**读口返回背景材料不打分——打分器住在过滤器插槽（二号槽）。**
五档评分制里只有档①②③⑤的检索材料从这里出；档④（缘分）是纯随机原语；
把分数算出来的 max(+ε)+配额 逻辑不在这个模块里。这样 pianist 未来接上时
替换的是打分器，不动读口——接口比实现活得久。

性能定位：全 SQL + 本地向量点积。零 LLM、零网络、零 embed 服务调用
（本机 embed 服务冷启动 19.8s/11条——事件向量一律由调用方随 event['vec']
送进来，读口绝不自己去算 embedding）。

表对齐点（真实 schema 抄自 server.py 与 scripts/soft_clusters.py）：
  - self_concept(id, field, content, updated_at, UNIQUE(field))   [server.py]
  - profiles(id, entity, ptype, content, updated_at, UNIQUE(entity,ptype)) [server.py]
  - topic_clusters(id, cluster_name, noun_freq, narrative_ids, last_active, avg_weight) [server.py]
  - emb_clusters(id, name, centroid, member_count, total_hits, created_at, updated_at)
    ——这张表不是 server.py 建的，是 scripts/soft_clusters.py 建的
    （centroid 是 JSON list 文本，如 '[1.0, 0.0, 0.0]'）
  - snapshots(id, content, created_at)                             [server.py]
  - threads(id, content, importance, emotional, recurrence, unresolved, weight, status, created_at, explored_at) [server.py]
  - pin_registry / ambient_feed：repo 没有，由本模块 ensure_* 幂等自建（见各函数）

对齐偏差（repo 没有的东西，夹具 tests/fixture_phase19_salience_read.py
用同款 DDL 自建了最小版）：
  - profiles 没有 user 标记位字段 → 约定 ptype='user' 为 user 层标记
    （server.py 的 ptype 是自由 TEXT，不冲突）。**待设计师拍板（开放问题#2）**

缓存策略（spec §4 说 now_state 走 snapshot 缓存）：读口本身无状态、无缓存，
纯函数语义；缓存交给调用方/插槽层做——读口每次都直读 DB，保证测试和并发
下语义稳定，也避免跨库连线的脏缓存。
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, timezone

# ── 常量（默认值，均为可拍板的旋钮） ──────────────────────────────
PTYPE_USER = "user"          # profiles 里 user 层的 ptype 标记（待设计师拍板·开放问题#2）
USER_LAYER_BONUS = 1.0       # others 架里 user 层命中的加分（叠加在基础 1.0 上）
EMB_MIN_SIM = 0.5            # emb 路由的粗下限：滤掉零/负相关簇（真向量近期邻居通常 0.5~0.75）
AMBIENT_QUOTA = {"world": 3, "china": 1}  # 余光档保留席 3+1（配额制非阈值制，spec 9/29 封版）
DEFAULT_EPS = 0.05           # 缘分档默认 ε（待实跑调·开放问题#4）


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _entities(event) -> list:
    """安全取事件实体列表：event 可为 None/缺键/元素为空串，都收敛成干净 list。"""
    if not isinstance(event, dict):
        return []
    ents = event.get("entities") or []
    if not isinstance(ents, (list, tuple)):
        return []
    return [e for e in ents if isinstance(e, str) and e.strip()]


# ═══════ 档① 结构共振：三架并行检索 ═══════

def _resonance_self(conn, entities, k: int) -> list:
    """self 架：事件实体命中自我概念（self_concept 表）。

    命中 = 实体以子串出现在 content 里。score = 命中的不同实体个数
    （多实体同中说明这条自我认知与事件共振面更宽）。表不存在→空 list。
    """
    if not entities or not _table_exists(conn, "self_concept"):
        return []
    hits = []
    try:
        rows = conn.execute(
            "SELECT field, content, updated_at FROM self_concept"
        ).fetchall()
    except Exception:
        return []
    for field, content, updated_at in rows:
        matched = [e for e in entities if e in (content or "")]
        if matched:
            hits.append({
                "field": field,
                "content": content,
                "matched": matched,
                "score": float(len(matched)),
                "updated_at": updated_at,
            })
    hits.sort(key=lambda h: (-h["score"], h["updated_at"] or ""))
    return hits[:k]


def _resonance_others(conn, entities, k: int) -> list:
    """others 架：事件实体命中人物画像（profiles 表，实体名精确匹配）。

    每条画像行一个命中。user 层（ptype='user'）加 USER_LAYER_BONUS，
    同分按 updated_at 新者前。表不存在→空 list。
    """
    if not entities or not _table_exists(conn, "profiles"):
        return []
    hits = []
    try:
        for ent in entities:
            rows = conn.execute(
                "SELECT ptype, content, updated_at FROM profiles WHERE entity=?",
                (ent,),
            ).fetchall()
            for ptype, content, updated_at in rows:
                score = 1.0 + (USER_LAYER_BONUS if ptype == PTYPE_USER else 0.0)
                hits.append({
                    "entity": ent,
                    "ptype": ptype,
                    "content": content,
                    "updated_at": updated_at,
                    "score": score,
                })
    except Exception:
        return []
    hits.sort(key=lambda h: (-h["score"], h["updated_at"] or ""))
    return hits[:k]


def _resonance_terrain(conn, event, k: int) -> list:
    """terrain 架：现象地形双路由。

    路由A topic：事件实体子串命中 topic_clusters.cluster_name（名字匹配）。
    路由B emb：事件向量（event['vec']，调用方注入的假/真向量均可）对
    emb_clusters.centroid（JSON list）做 cosine 最近邻，低于 EMB_MIN_SIM
    的簇丢弃（零/负相关不是共振）；event 无 vec 则整条 emb 路由跳过。
    两条路由各取 top-k 后合并，条目带 route 标记供打分器分轨处理。
    任一表不存在→该路由优雅返回空。
    """
    entities = _entities(event)
    out = []

    # 路由A：topic 名字匹配
    if entities and _table_exists(conn, "topic_clusters"):
        try:
            rows = conn.execute(
                "SELECT cluster_name, noun_freq, avg_weight FROM topic_clusters"
            ).fetchall()
        except Exception:
            rows = []
        t_hits = []
        for name, noun_freq, avg_weight in rows:
            matched = [e for e in entities if e in (name or "")]
            if matched:
                t_hits.append({
                    "route": "topic",
                    "name": name,
                    "matched": matched,
                    "score": float(len(matched)),
                    "noun_freq": noun_freq,
                    "avg_weight": avg_weight,
                })
        t_hits.sort(key=lambda h: (-h["score"], h["name"] or ""))
        out.extend(t_hits[:k])

    # 路由B：emb 向量最近邻
    vec = event.get("vec") if isinstance(event, dict) else None
    if vec and _table_exists(conn, "emb_clusters"):
        try:
            rows = conn.execute(
                "SELECT name, centroid FROM emb_clusters"
            ).fetchall()
        except Exception:
            rows = []
        e_hits = []
        for name, centroid in rows:
            try:
                c = json.loads(centroid) if isinstance(centroid, str) else centroid
            except (ValueError, TypeError):
                continue
            sim = _cosine(vec, c)
            if sim >= EMB_MIN_SIM:
                e_hits.append({"route": "emb", "name": name, "score": float(sim)})
        e_hits.sort(key=lambda h: -h["score"])
        out.extend(e_hits[:k])

    return out


def _cosine(a, b) -> float:
    """纯 Python cosine；零向量/维度不匹配→0.0（不算共振）。"""
    try:
        if len(a) != len(b):
            return 0.0
        dot = sum(float(x) * float(y) for x, y in zip(a, b))
        na = sum(float(x) ** 2 for x in a) ** 0.5
        nb = sum(float(y) ** 2 for y in b) ** 0.5
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)
    except (TypeError, ValueError):
        return 0.0


# ═══════ 档③ social_weight：重要他人分层 ═══════

def _social_weight(conn, entities) -> dict:
    """事件实体的社交分层：{entity: 'user' | 'other'}。

    只列在 profiles 里真实有画像行的实体（路人不上榜）。user 层判定：
    该实体存在 ptype='user' 的画像行。**user 标记位为约定 ptype 字段，
    server.py 现无此字段——待设计师拍板（开放问题#2：标记位 vs 独立白名单）。**
    同意拓扑（consent）不在这里——那是写口执法的事，读口只出分层材料。
    """
    if not entities or not _table_exists(conn, "profiles"):
        return {}
    out = {}
    try:
        for ent in entities:
            row = conn.execute(
                "SELECT 1 FROM profiles WHERE entity=? AND ptype=? LIMIT 1",
                (ent, PTYPE_USER),
            ).fetchone()
            has_profile = conn.execute(
                "SELECT 1 FROM profiles WHERE entity=? LIMIT 1", (ent,)
            ).fetchone()
            if has_profile:
                out[ent] = "user" if row else "other"
    except Exception:
        return {}
    return out


# ═══════ 档② pin registry（硬约束配置层，非记忆） ═══════

def ensure_pin_registry(conn) -> None:
    """幂等建 pin 注册表。

    pin 与 threads 的区别：pin 是可随时改的硬约束配置，thread 是 DREAM
    产出的探索方向——pin 不住记忆库，住配置层（spec 开放问题#1）。
    结构=设计师拍板 2026-10-02（同意hui草案）：一条 = 内容 + 权重 +
    生效(created_at，插入即活) + 过期(expires_at 可空=永久；
    手动撤=active 置 0)。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pin_registry(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pin_text TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            active INTEGER DEFAULT 1,
            created_at TEXT,
            expires_at TEXT
        )
    """)
    # 旧库补列（幂等；列已存在时 ALTER 报错吞掉，参照 view_state 迁移同款）
    try:
        conn.execute("ALTER TABLE pin_registry ADD COLUMN expires_at TEXT")
    except Exception:
        pass
    conn.commit()


def pins_snapshot(conn, now=None) -> list:
    """当前 pin 快照：实时读 active=1 且未过期的行（pin 可变性是硬约束，不做缓存）。

    now: ISO 字符串，缺省取当前 UTC。expires_at 为空 = 永久
    （设计师拍板 2026-10-02：过期可放可不放，不放就手动撤）。
    """
    if not _table_exists(conn, "pin_registry"):
        return []
    if now is None:
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = conn.execute(
            "SELECT pin_text, weight, created_at, expires_at FROM pin_registry "
            "WHERE active=1 AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY id",
            (now,),
        ).fetchall()
    except Exception:
        return []
    return [
        {"pin_text": r[0], "weight": float(r[1]) if r[1] is not None else 1.0,
         "created_at": r[2], "expires_at": r[3]}
        for r in rows
    ]


# ═══════ 档⑤ 余光档：ambient feed 配额（3+1 保留席） ═══════

def ensure_ambient_feed(conn) -> None:
    """幂等建余光档缓存表。

    配额制非阈值制：3+1 是保留席，不参与相关性排名——防的就是
    「相关性阈值把世界变化全滤掉」的活在过去病（spec 9/29 封版）。
    数据源接入只留接口不实现爬取：ingest_ambient() 是写入侧接口，
    实际取数（倾向复用早报 cron 的扫描链路，一份采集多层消费）
    **待定——开放问题#3**。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ambient_feed(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            region TEXT NOT NULL,
            headline TEXT NOT NULL,
            source TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()


def ingest_ambient(conn, region: str, headlines, source: str = "", fetched_at: str = None) -> int:
    """余光档写入侧接口（只留接口，不实现爬取——开放问题#3）。

    早报 cron / 任何采集器拿到头部头条后调这里落缓存。返回写入行数。
    """
    ensure_ambient_feed(conn)
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc).isoformat()
    n = 0
    for h in headlines or []:
        if not h:
            continue
        conn.execute(
            "INSERT INTO ambient_feed(region, headline, source, fetched_at) VALUES(?,?,?,?)",
            (region, str(h), source, fetched_at),
        )
        n += 1
    conn.commit()
    return n


def ambient_quota(conn, date: str = None, quotas: dict = None) -> dict:
    """余光档当日配额状态：{'world': {filled, quota:3}, 'china': {filled, quota:1}}。

    date 为 'YYYY-MM-DD'（默认 UTC 今天）；只数当日行。表不存在→filled=0。
    地域配额可通过 quotas 覆盖（世界 3 + 中国 1 是默认保留席）。
    """
    quotas = quotas or AMBIENT_QUOTA
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = {region: {"filled": 0, "quota": int(q)} for region, q in quotas.items()}
    if not _table_exists(conn, "ambient_feed"):
        return out
    try:
        for region in quotas:
            row = conn.execute(
                "SELECT COUNT(*) FROM ambient_feed WHERE region=? AND fetched_at LIKE ?",
                (region, date + "%"),
            ).fetchone()
            out[region]["filled"] = int(row[0]) if row else 0
    except Exception:
        pass
    return out


# ═══════ 档④ 缘分档原语（纯随机，无表） ═══════

def luck_gate(rng: random.Random, eps: float = DEFAULT_EPS) -> bool:
    """缘分档原语：小概率给低分事件放行（「说不定有惊喜呢」）。

    返回 bool。eps=0 → 永不放行（全 False）；eps=1 → 永远放行（全 True）。
    同 seed 的 rng 序列可复现——测试/审计都需要这个性质。
    ε 默认 0.05 起步，**待实跑调（开放问题#4）**。防的是回音室：随机无方向，
    与余光档的确定性有方向（外部头部）互补不互替。
    """
    return bool(rng.random() < eps)


# ═══════ now_state：当下上下文（「她在睡觉」这类状态的来源） ═══════

def _now_state(conn) -> dict:
    """最新 snapshot + 活跃 threads 概要。相关表不存在→空 dict。"""
    out = {}
    try:
        if _table_exists(conn, "snapshots"):
            row = conn.execute(
                "SELECT content, created_at FROM snapshots ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if row:
                out["snapshot"] = {"content": row[0], "created_at": row[1]}
        if _table_exists(conn, "threads"):
            rows = conn.execute(
                "SELECT content, importance, unresolved, weight, created_at FROM threads "
                "WHERE status='open' ORDER BY created_at DESC"
            ).fetchall()
            out["threads"] = [
                {"content": r[0], "importance": r[1], "unresolved": r[2],
                 "weight": r[3], "created_at": r[4]}
                for r in rows
            ]
    except Exception:
        return out
    return out


# ═══════ 总读口 ═══════

def salience_context(conn, event, *, k: int = 3, date: str = None) -> dict:
    """显著性过滤器的背景材料读口（每事件一调）。

    读口返回背景材料，不返回分数，打分器住在过滤器插槽（二号槽）——
    score 字段只是各架的原始检索强度，不是五档制的最终分。

    event: {'entities': [...], 'vec': [float, ...]（可选，缺省跳过 emb 路由）}
    k:     每架/每路由的 top-k
    date:  余光档配额的考察日（默认 UTC 今天），测试可注入固定日期

    返回五档契约：
      resonance{self/others/terrain}  档① 三架检索材料（terrain 带 route 标记）
      pins                            档② 当前 pin 快照（active 行，实时读）
      social_weight                   档③ {entity: 'user'|'other'} 分层
      ambient_quota                   档⑤ 余光档配额状态（今日 3+1 是否已满）
      now_state                       最新 snapshot + 活跃 threads
    """
    entities = _entities(event)
    return {
        "resonance": {
            "self": _resonance_self(conn, entities, k),
            "others": _resonance_others(conn, entities, k),
            "terrain": _resonance_terrain(conn, event, k),
        },
        "pins": pins_snapshot(conn),
        "social_weight": _social_weight(conn, entities),
        "ambient_quota": ambient_quota(conn, date),
        "now_state": _now_state(conn),
    }
