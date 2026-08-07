#!/usr/bin/env python3
"""
Tideline Memory v2.3 — Script Layer (deterministic, no LLM, no token cost)

Three jobs:
1. jieba noun-frequency → TF-IDF filtering → co-occurrence clustering → topic_clusters table
2. Weight backfill for legacy narratives (no weight)
3. Weight normalization sweep (anti-inflation)

Usage:
  python3 scripts/dream_scripts.py clusters     # rebuild topic clusters
  python3 scripts/dream_scripts.py weights      # backfill + normalize weights
  python3 scripts/dream_scripts.py all          # run everything

Runs independently of MCP server. Safe to run anytime.
Requires: jieba (system python3.12)
"""

import os, sys, json, sqlite3, re, math
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict
import jieba
import jieba.posseg as pseg

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))

def _compute_weight(imp, emo, rec, unr):
    """Multi-dimensional weight → normalized 0-1."""
    if None in (imp, emo, rec, unr):
        imp = imp or 3; emo = emo or 3; rec = rec or 3; unr = unr or 3
    raw = imp * 0.35 + emo * 0.25 + rec * 0.25 + unr * 0.15
    return raw / 5.0

def _normalize_weights(c, window=20):
    """Distribution normalization: if recent avg weight > 0.7, compress to spread."""
    rows = c.execute(
        "SELECT id, weight FROM narratives WHERE weight IS NOT NULL ORDER BY created_at DESC LIMIT ?",
        (window,)
    ).fetchall()
    if len(rows) < 5:
        return
    weights = [r["weight"] for r in rows]
    avg = sum(weights) / len(weights)
    if avg <= 0.7:
        return
    scale = 0.6 / avg
    for r in rows:
        new_w = max(0.0, min(1.0, r["weight"] * scale))
        c.execute("UPDATE narratives SET weight = ? WHERE id = ?", (new_w, r["id"]))

# ─── Helpers ─────────────────────────────────────────────
def _db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# ─── Union-Find (no external deps) ───────────────────────
class UnionFind:
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

    def groups(self):
        g = defaultdict(set)
        for x in self.parent:
            g[self.find(x)].add(x)
        return g

# ─── 1. Topic Clustering (TF-IDF + co-occurrence) ────────

NOUN_TAGS = {'n', 'nr', 'ns', 'nt', 'nz', 'vn', 'ng'}

# Domain stopword list — high-frequency, low-discrimination words
# that jieba correctly tags as nouns but carry no topic signal
DOMAIN_STOPWORDS = frozenset("""
用户 核心 东西 问题 模式 结构 框架 方式 方向 信号 分析 关系 空间 状态 数据
内容 机制 体验 结论 实验 设计 信息 区分 关键 时候 时间 对话 系统 工具
记录 反应 模型 架构 质感 概念 角度 层面 意义 价值 部分 整体 觉察 观察
发现 思考 理解 认识 本质 根源 逻辑 能力 可能 现实 目标 结果 过程 细节
感受 情绪 感觉 意识 变化 区别 特征 类型 阶段 起点 终点 路径 方法 理论
原因 理由 事实 理想 实际 行为 动机 驱动 内在 外在 自我 对象 范畴 规律
规则 原则 标准 尺度 维度 参数 配置 功能 特性 属性 条件 前提 基础 假设
背景 场景 环境 语境 情境 情况 场合 时刻 瞬间 片段 阶层 层级 级别 等级
差异 相似 共同 普遍 特殊 个别 一般 具体 抽象 复杂 简单 明显 隐含
潜在 直接 间接 核心 边缘 中心 外围 表面 深层 浅层 高层 低层 中层
无法 产生 自动 修正 全部 深度 质量 工作 地方
""".split())

# English stopword filter — common words jieba tags as 'eng'
ENGLISH_STOPWORDS = frozenset("""
the a an is are was were be been being have has had do does did
will would should could may might must can to of in on at by with
for from up out as it its this that these those there here
not no nor so than too very can just also only own same other some
such more most many much few less least one two three first last
what which who whom whose when where why how all any both each
get got make made take took come came go went see saw know knew
think thought want wanted need needed like liked use used try tried
about above across after again against among around because before
below between during except inside near off over since through under
until without vs etc aka ie eg
you it he she they we me him her them us our your their his hers
my mine ours theirs am being having doing saying going getting
and or but if then else when while where what which how why who
Friday Monday Tuesday Wednesday Thursday Saturday Sunday January February
March April May June July August September October November December
Can Fit Gray Let Set Put Say Way Day Year Week Month Time
The You It He She They We Me Him Her Them Us Our Your Their His
And Or But If Then Else When While Where What Which How Why Who
Are Were Been Being Have Has Had Do Does Did Will Would Should
Could May Might Must For From About Above Across After Again Against
Among Around Because Before Below Between During Except Inside Near
Off Over Since Through Under Until Without One Two Three First Last
""".split())

def extract_nouns(text):
    """Extract meaningful nouns from mixed CN/EN text using jieba."""
    nouns = []
    if not text:
        return nouns
    for word, flag in pseg.cut(text):
        w = word.strip()
        if not w:
            continue
        # Chinese nouns
        if flag in NOUN_TAGS and len(w) >= 2 and w not in DOMAIN_STOPWORDS:
            nouns.append(w)
        # English words (jieba tags as 'eng')
        elif flag == 'eng' and len(w) >= 3:
            wl = w.lower()
            if wl not in ENGLISH_STOPWORDS and w not in ENGLISH_STOPWORDS:
                # Keep proper nouns (Capitalized, 3+ chars) or regular words (4+ lowercase)
                if w[0].isupper() and wl.isalpha() and len(wl) >= 3:
                    nouns.append(wl)
                elif wl.isalpha() and len(wl) >= 4:
                    nouns.append(wl)
    return nouns


def build_clusters(min_freq=3, max_df_ratio=0.20, merge=False, min_jaccard=0.7):
    """
    TF-IDF filtered topic clustering.

    Pipeline:
    1. Extract nouns per narrative (jieba POS tagging)
    2. Compute document frequency (DF) per noun
    3. Filter: keep nouns with DF >= min_freq AND DF < max_df_ratio * N
       (auto-removes generic words like 用户/核心/问题 via DF cap)
    4. Each surviving noun = one topic cluster (precision over merging)
    5. Optional: merge near-synonymous nouns via Jaccard similarity
       (disabled by default — with ~250 narratives, singleton clusters
       are more precise than merged mega-clusters)

    At scale (>1000 narratives), enable merge=True to consolidate
    co-occurring noun groups.
    """
    c = _db()
    rows = c.execute(
        "SELECT id, content, gesture, context_layer, cognition_direction FROM narratives"
    ).fetchall()

    total_docs = len(rows)
    if total_docs == 0:
        print("No narratives found.")
        c.close()
        return

    # Step 1-2: Extract nouns, build per-doc noun sets, compute DF
    doc_nouns = {}          # narrative_id -> [nouns]
    noun_to_docs = defaultdict(set)  # noun -> set of narrative_ids

    for r in rows:
        text_parts = [r["content"] or "", r["gesture"] or "",
                      r["context_layer"] or "", r["cognition_direction"] or ""]
        text = " ".join(text_parts)
        nouns = extract_nouns(text)
        doc_nouns[r["id"]] = nouns
        unique_in_doc = set(nouns)
        for n in unique_in_doc:
            noun_to_docs[n].add(r["id"])

    # Step 3: Filter nouns — DF threshold + max_df_ratio cap
    max_df = max(min_freq + 1, int(total_docs * max_df_ratio))
    kept_nouns = {n for n, docs in noun_to_docs.items()
                  if len(docs) >= min_freq and len(docs) < max_df}

    filtered_generic = sorted(
        {n for n, docs in noun_to_docs.items() if len(docs) >= max_df}
    )
    filtered_rare = len(noun_to_docs) - len(kept_nouns) - len(filtered_generic)

    # Step 4: Compute IDF per noun
    noun_idf = {}
    for n in kept_nouns:
        noun_idf[n] = math.log(total_docs / len(noun_to_docs[n]))

    # Step 5-6: Optional merge via Jaccard similarity
    cooccur_pairs_count = 0
    merged_count = 0
    if merge:
        cooccur_pairs = set()
        for nid, nouns in doc_nouns.items():
            unique = sorted(set(nouns) & kept_nouns)
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    cooccur_pairs.add((unique[i], unique[j]))
        cooccur_pairs_count = len(cooccur_pairs)

        uf = UnionFind()
        for n in kept_nouns:
            uf.find(n)

        for a, b in cooccur_pairs:
            docs_a = noun_to_docs[a]
            docs_b = noun_to_docs[b]
            intersection = len(docs_a & docs_b)
            union = len(docs_a | docs_b)
            jaccard = intersection / union if union > 0 else 0
            if jaccard >= min_jaccard:
                uf.union(a, b)
                merged_count += 1
    else:
        uf = None

    # Step 7: Build clusters
    if merge and uf is not None:
        groups = uf.groups()
    else:
        # Each kept noun is its own cluster
        groups = {n: {n} for n in kept_nouns}

    clusters = []
    for root, members in groups.items():
        cluster_nids = set()
        member_idfs = []
        for n in members:
            cluster_nids |= noun_to_docs[n]
            member_idfs.append((noun_idf[n], n))

        if not cluster_nids:
            continue

        member_idfs.sort(reverse=True)
        name = member_idfs[0][1]
        if len(members) > 1:
            alt_names = [m[1] for m in member_idfs[:3]]
            display_name = " / ".join(alt_names)
        else:
            display_name = name

        clusters.append({
            "name": display_name,
            "primary_noun": name,
            "members": sorted(members),
            "member_count": len(members),
            "narrative_ids": sorted(cluster_nids),
            "narrative_count": len(cluster_nids),
        })

    clusters.sort(key=lambda x: -x["narrative_count"])

    # Write to topic_clusters
    c.execute("DELETE FROM topic_clusters")

    for cl in clusters:
        nids = cl["narrative_ids"]
        placeholders = ",".join("?" * len(nids))
        avg_w_row = c.execute(
            f"SELECT AVG(weight) as aw FROM narratives WHERE id IN ({placeholders}) AND weight IS NOT NULL",
            nids
        ).fetchone()
        avg_w = avg_w_row["aw"] if avg_w_row["aw"] else 0.0

        recent = c.execute(
            f"SELECT MAX(created_at) as la FROM narratives WHERE id IN ({placeholders})",
            nids
        ).fetchone()
        last_active = recent["la"] if recent["la"] else None

        # Store member nouns as JSON for debugging/display
        meta = json.dumps({"primary": cl["primary_noun"], "members": cl["members"]},
                          ensure_ascii=False)

        c.execute(
            """INSERT OR REPLACE INTO topic_clusters
               (cluster_name, noun_freq, narrative_ids, last_active, avg_weight)
               VALUES (?,?,?,?,?)""",
            (cl["name"], cl["narrative_count"], json.dumps(nids), last_active, avg_w),
        )

    c.commit()

    # Stats
    total_clusters = len(clusters)
    singletons = sum(1 for cl in clusters if cl["member_count"] == 1)
    multi_noun = sum(1 for cl in clusters if cl["member_count"] >= 2)
    big_clusters = [cl for cl in clusters if cl["narrative_count"] >= 10]

    print(f"✅ Topic clusters rebuilt: {total_clusters} clusters from {total_docs} narratives")
    print(f"   Nouns extracted: {len(noun_to_docs)} | Kept (TF-IDF filtered): {len(kept_nouns)}")
    print(f"   Generic filtered (DF > {max_df}): {len(filtered_generic)} → {sorted(filtered_generic)[:10]}")
    print(f"   Rare filtered (DF < {min_freq}): {filtered_rare}")
    print(f"   Co-occurrence pairs: {cooccur_pairs_count} | Jaccard merges (≥{min_jaccard}): {merged_count}")
    print(f"   Multi-noun merged clusters: {multi_noun} | Single-noun clusters: {singletons}")
    print(f"\n   Top {min(25, len(big_clusters))} clusters by narrative count:")
    for cl in big_clusters[:25]:
        mem_str = f" [{cl['member_count']} nouns]" if cl["member_count"] > 1 else ""
        print(f"   {cl['name']:35s} | narr={cl['narrative_count']:3d} {mem_str}")

    c.close()


# ─── 2. Weight Backfill ──────────────────────────────────
def backfill_weights():
    """Assign weights to legacy narratives that have no weight yet."""
    c = _db()
    rows = c.execute(
        "SELECT id, content, ntype, tags FROM narratives WHERE weight IS NULL"
    ).fetchall()

    if not rows:
        print("✅ All narratives already have weights. Nothing to backfill.")
        c.close()
        return

    count = 0
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else []
        content = r["content"] or ""
        ntype = r["ntype"] or "general"

        imp = 3
        emo = 3
        rec = 2
        unr = 2

        if ntype in ("moment", "self_reflection"):
            imp = 4
        if ntype == "general":
            imp = 2

        emotional_markers = ["哭", "笑", "生气", "开心", "难过", "害怕", "爱", "喜欢",
                           "讨厌", "累", "痛苦", "温暖", "心疼", "撒娇"]
        if any(m in content for m in emotional_markers):
            emo = 4

        if tags and len(tags) >= 2:
            rec = 3

        weight = _compute_weight(imp, emo, rec, unr)
        c.execute("UPDATE narratives SET weight=?, importance=?, emotional=?, recurrence=?, unresolved=? WHERE id=?",
                  (weight, imp, emo, rec, unr, r["id"]))
        count += 1

    c.commit()
    c.close()
    print(f"✅ Weight backfill: {count} narratives assigned weights")

# ─── 2b. Recurrence Refresh ──────────────────────────────
def refresh_recurrence():
    """Recalculate recurrence for ALL narratives based on current tag frequencies."""
    c = _db()

    # Build tag frequency map
    all_tags = {}
    rows = c.execute("SELECT id, tags FROM narratives WHERE tags IS NOT NULL").fetchall()
    for r in rows:
        try:
            tags = json.loads(r["tags"])
            for t in tags:
                all_tags[t] = all_tags.get(t, 0) + 1
        except (json.JSONDecodeError, TypeError):
            pass

    def _rec_from_freq(freq):
        if freq <= 1: return 1
        elif freq <= 3: return 2
        elif freq <= 6: return 3
        elif freq <= 11: return 4
        else: return 5

    updated = 0
    for r in rows:
        try:
            tags = json.loads(r["tags"])
        except:
            tags = []
        if not tags:
            continue

        co_count = max(all_tags.get(t, 0) - 1 for t in tags) if tags else 0
        new_rec = _rec_from_freq(co_count)

        full = c.execute(
            "SELECT importance, emotional, unresolved FROM narratives WHERE id = ?",
            (r["id"],),
        ).fetchone()
        if not full:
            continue

        new_weight = _compute_weight(full["importance"], full["emotional"], new_rec, full["unresolved"])
        c.execute("UPDATE narratives SET recurrence = ?, weight = ? WHERE id = ?", (new_rec, new_weight, r["id"]))
        updated += 1

    c.commit()
    c.close()
    print(f"✅ Recurrence refreshed: {updated} narratives updated from {len(all_tags)} unique tags")


# ─── 3. Weight Normalization ─────────────────────────────
def normalize_all():
    """Run distribution normalization across all narratives."""
    c = _db()
    before = c.execute("SELECT AVG(weight) as aw FROM narratives WHERE weight IS NOT NULL").fetchone()["aw"]
    _normalize_weights(c, window=50)
    c.commit()
    after = c.execute("SELECT AVG(weight) as aw FROM narratives WHERE weight IS NOT NULL").fetchone()["aw"]
    dist = c.execute(
        "SELECT CASE WHEN weight >= 0.7 THEN 'high' WHEN weight >= 0.4 THEN 'mid' ELSE 'low' END as band, COUNT(*) as n FROM narratives WHERE weight IS NOT NULL GROUP BY band"
    ).fetchall()
    c.close()

    print(f"✅ Normalization: avg {before:.3f} → {after:.3f}" if before and after else "✅ Normalization done")
    for d in dist:
        print(f"   {d['band']}: {d['n']}条")

# ─── Main ────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

    if cmd in ("clusters", "all"):
        print("\n═══ Topic Clustering ═══")
        build_clusters()

    if cmd in ("weights", "all"):
        print("\n═══ Weight Backfill ═══")
        backfill_weights()
        print("\n═══ Recurrence Refresh ═══")
        refresh_recurrence()
        print("\n═══ Weight Normalization ═══")
        normalize_all()

    if cmd == "all":
        print("\n✅ All scripts complete.")
    elif cmd not in ("clusters", "weights", "recurrence"):
        print(f"Unknown command: {cmd}")
        print("Usage: python3 dream_scripts.py [clusters|weights|recurrence|all]")
