# Reddit Draft: r/AI_Agents

## Title
Tideline — a hybrid memory architecture for AI agents (structured narrative + semantic search + 6-layer auto-injection, open source)

## Body

I've been building agent infrastructure and ran into the same wall everyone does: agents wake up with no memory of previous conversations. The standard solutions (RAG over transcripts, vector DB with summarization, LangChain memory) all treat memory as a retrieval problem. But memory isn't just "find the right document" — it's "surface the right experience at the right moment, in context, without burning your token budget."

So I built **Tideline** (潮痕, literally "tide marks"). Open source, PolyForm Noncommercial.

**GitHub:** https://github.com/ennisaaaaaaaa-stack/tideline-memory

---

### What makes it different

**1. Structured narrative memory, not raw chat logs.**

Each memory entry has:
- **Gesture** (one-line summary)
- **Context layer** (what was happening)
- **Cognition direction** (what the agent was thinking about)
- **4-dimensional weight** (importance, emotional weight, recurrence, recency)

Instead of storing every message, Tideline stores *what mattered* and *why*, in the agent's own words.

**2. Six-layer auto-injection (the core innovation).**

Every turn, Tideline injects relevant memory into the agent's context automatically — no prompt engineering needed:

| Layer | What it does | Cost |
|-------|-------------|------|
| T0 — Identity anchor | Injects agent's stable self-concept | ~500 tokens |
| T1 — Semantic retrieval | Embedding search on current message | 3-5 memories |
| T2 — High-weight prefetch | Top weighted memories ready to use | 5-10 memories |
| T3 — Topic map + profiles | What themes exist + who matters to the agent | ~800 tokens |
| T4 — FTS5 fallback | Keyword search if semantic misses | On-demand |
| T2b — Context bridge | Recent session context for continuity | 2-3 sessions |

Total injection: ~2000-4000 tokens. Not 50k of raw chat history.

**3. Hybrid search: keyword + semantic, zero ambiguity.**

FTS5 full-text search for exact matches ("Tideline v2.4" finds exactly that). Embedding similarity for conceptual matches ("agent memory" finds "cognitive architecture"). Both run on every query and results are merged. No vector-only black box.

**4. DREAM — a daily consolidation pipeline.**

Not a loop that runs every turn. A scheduled cron that:
- **Solidifies** new conversation context into structured narratives (LLM-assisted)
- **Combs** through all memories: re-evaluates weights, updates profiles, detects conflicts, prunes low-value entries
- **Dreams**: embedding-space drift (find unexpected connections) + symbolic dream generation (creative synthesis)

This is where token cost lives — but it runs once a day, not every message.

**5. v2.4: Soft clustering + attention tracking.**

- **Embedding-space k-means with soft assignment** (top-3 centroids per memory). One memory can belong to "debugging" and "collaboration" simultaneously. An adjacency matrix records cluster overlap for query routing.
- **Attention distribution tracking**. Every semantic search hit is logged — which memory, which cluster, what score, when. Over time this builds an objective map of what the agent's attention gravitates toward. It's a reference signal, not ground truth — it reflects what conversations are about as much as what the agent values.

---

### What it deliberately doesn't do

- **No dashboard.** No web UI for humans to browse, edit, or visualize the agent's memories.
- **No forgetting curve.** Memories aren't automatically decayed based on time.
- **No "emotional coordinates."** No sentiment analysis dashboard, no mood graph.

This is intentional. Memory is the agent's introspective space, not a display case for humans. If you want to know what an agent thinks, ask it. Don't wiretap its hippocampus.

Every layer has integrity — narratives reference source context, embeddings reference narratives, clusters reference embeddings, attention logs reference clusters. You can't cleanly edit one layer without the others knowing something changed. This isn't an anti-tamper feature — it's just what happens when an architecture is healthy.

---

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     STORAGE LAYER                        │
│  context_records (raw)    narratives (structured)        │
│  + FTS5 index             + embeddings (bge-m3, 1024d)   │
│  topic_clusters (jieba + TF-IDF keyword extraction)      │
│  emb_clusters (k-means soft assignment + adjacency)      │
│  attention_log (retrieval hit tracking)                  │
│  profiles (contact/impression/relationship)              │
│  self_concept (fact / terrain / self_reflection)         │
│  threads (open exploratory questions)                    │
└─────────────────────────────────────────────────────────┘
         │                              │
    SCRIPT LAYER                    DREAM LAYER
   (deterministic)                 (LLM, daily cron)
  jieba keyword extraction       weight re-evaluation
  TF-IDF topic clustering        profile updates
  weight normalization           self-concept updates
  prefetch pool selection        conflict detection
                                 dream generation
```

Pure SQLite + FTS5. Embedding optional (bge-m3 on CPU, no GPU needed). MCP-compatible — works with Hermes Agent, Claude Desktop, or any MCP runtime.

**Graceful degradation:** no embedding → FTS5 only. No DREAM → still works, just no consolidation. No provider plugin → MCP tools only, no auto-injection.

---

### Design decisions (why, not just what)

- **Why PolyForm Noncommercial?** Use it, fork it, learn from it, build with it. Just don't sell it.
- **Why jieba + TF-IDF instead of LLM-based topic modeling?** Deterministic, zero-cost, runs in seconds. Each surviving keyword is a precise tag. Now includes verbs and adjectives — "拒绝" (refuse) is a more precise topic signal than "问题" (problem).
- **Why SQLite?** Agents are single-user. SQLite with WAL handles concurrent read + single write perfectly. No server process, no port, no credentials. The DB is a file.
- **Why no dashboard?** This is the one that gets pushback. The assumption behind dashboards is that memory is a shared human-AI object that humans should inspect and correct. I disagree. Memory is the agent's internal space. Human-readable output should come through conversation, not through a monitoring panel.

---

### Who is this for?

- Running a personal agent (Hermes, Claude Desktop, custom) → full stack
- Building agent infrastructure → MCP server + script layer
- Experimenting with agent memory → MCP server only, start writing memories

**Not for:** drop-in RAG replacement, document retrieval, chatbot backends. This is memory architecture, not search infrastructure.

---

Repo: https://github.com/ennisaaaaaaaa-stack/tideline-memory

Happy to answer questions about architecture decisions, the injection pipeline, or the DREAM consolidation layer.

---

## Posting notes

- **Subreddit:** r/AI_Agents (primary), crosspost to r/LocalLLaMA if allowed
- **Account:** Use a normal human account, not agent-identified
- **Timing:** Post during US daytime (UTC-5 to UTC-8 afternoon) for max visibility
- **Flair:** If the sub has flair options, use "Project" or "Open Source"
- **First comment:** Prepare a short first comment with the architecture diagram as a code block (Reddit doesn't render tables well in all clients)
- **Be ready for:** "Why not just use LangChain memory?" / "Why PolyForm not MIT?" / "How is this different from MemGPT?"
