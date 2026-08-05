# Tideline 潮痕

A memory architecture for AI agents that thinks about memory the way tides think about the shore — everything that washes in is preserved; what matters is what gets left behind when the water recedes.

Built as an [MCP server](https://modelcontextprotocol.io) with a deterministic script layer (no LLM, no token cost) and an optional DREAM layer (LLM-driven consolidation). Designed for [Hermes Agent](https://hermes-agent.nousresearch.com) but works with any MCP-compatible runtime.

## Why

Agent memory isn't a storage problem — it's a retrieval problem. Most of the time, agents don't forget because data is lost; they forget because the right memory doesn't surface at the right moment. Tideline is built around this insight: **store everything, retrieve what matters, inject it before you ask.**

### Core principles

1. **Storage is infinite, context window is finite.** An agent's storage layer (SQLite + embeddings) is an external hard drive — no capacity pressure, no need to forget. Only the context window needs brain-like management.

2. **Write-as-formatting.** Memories aren't stored as free-text blobs. Each narrative memory has structure: a *gesture* (what happened, one sentence with tone), *context* (the backdrop), *cognition_direction* (where the thinking was heading), plus multi-dimensional weights.

3. **Narrative as entry point, not archive.** A structured narrative memory is the front door. Behind it, `source_links` index the full raw context — the texture and detail that a summary would lose. You retrieve the narrative for the signal, follow the links for the质感 (texture).

4. **Deterministic + LLM layers, decoupled.** Topic clustering, weight normalization, and prefetch pool selection are pure computation (jieba + TF-IDF + SQL). Only consolidation tasks (profile updates, self-concept abstraction, conflict detection, dream generation) need LLM calls, running on a daily cron. This keeps token costs near-zero for high-frequency operations.

## Architecture

```
                    WRITE (memory_write)
                         │
          gesture · context · cognition_direction
          weights (importance · emotional · recurrence · unresolved)
          related_entities · source_links · tags
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              STORE (SQLite, unlimited)               │
│                                                     │
│  narratives    context       profiles   self_concept │
│  (structured   (full raw     (fact/      (fact/      │
│   memories)    sessions +    impression/ terrain/    │
│                FTS5 index)   relationship) reflection)│
│                                                     │
│  topic_clusters (TF-IDF filtered noun → narrative    │
│                  groups, rebuilt by script layer)    │
│  threads (DREAM-produced exploration directions)     │
└─────────────────────────────────────────────────────┘
          │                            │
    SCRIPT LAYER                   DREAM LAYER
   (deterministic)               (LLM, daily cron)
          │                            │
  jieba noun extraction        weight re-evaluation
  TF-IDF topic clustering      profile updates
  weight normalization         self-concept updates
  prefetch pool selection      conflict detection
  (no token cost,             thread generation
   runs anytime)              three-layer dream system
          │                            │
          └──────────┬─────────────────┘
                     ▼
              INJECT (into context)
                     │
    T0: identity anchor (SOUL.md + self_concept)
    T1: session bridge (recent context)
    T2: prefetch cache (high-weight + topic-relevant)
    T3: memory map (cluster index + profiles)
    T4: active retrieval (embedding + FTS5, fallback)
```

### Layer 0 — Solidification (固化)

Before the DREAM three layers (combing / drifting / dreaming), a **Layer 0** runs first: a deterministic scanner detects unindexed conversation context so it can be solidified into narrative memories.

- **`scan_unindexed.py`** — two-track detector (no LLM, zero token cost):
  - **Track A**: narratives with empty `source_links` (potentially unindexed)
  - **Track B**: timestamp gap — conversation entries (`sync_turn`) after the latest narrative, grouped into time-proximity chunks
- **`source_links` tracking** — every new narrative back-links to the raw context IDs it came from. The narrative is the signal; `source_links` are the trail back to the texture.

The scanner's markdown output feeds into [`prompts/dream_solidify.md`](prompts/dream_solidify.md), which guides the LLM through judgment (what's worth keeping) and writing (structured `memory_write` with `source_links` filled).

## Features

### Memory types

| Tool | Purpose |
|------|---------|
| `memory_write` | Write structured narrative memory with weights |
| `memory_search` | Hybrid search: FTS5 keyword + embedding semantic |
| `memory_recall` | Browse recent narratives, filter by type/tags |
| `context_search` | Search raw context (full sessions) |
| `context_timeline` | Browse context chronologically |
| `context_record` | Store context to the timeline |
| `memory_write_profile` | Write/update entity profiles (fact/impression/relationship) |
| `memory_read_profiles` | Read entity profiles |
| `memory_write_self_concept` | Write/update self-concept (fact/terrain/self_reflection) |
| `memory_read_self_concept` | Read self-concept |
| `memory_write_snapshot` | Store state snapshot (daily status note) |
| `memory_read_snapshot` | Read latest state snapshot |
| `memory_write_thread` | Create exploration thread (DREAM output) |
| `memory_read_threads` | Browse threads by status |

### Multi-dimensional weight system

Each narrative memory is scored on four dimensions (1-5), combined into a normalized weight:

| Dimension | Question it answers |
|-----------|-------------------|
| importance | How much does this affect core relationships/projects? |
| emotional | How intense was the moment? |
| recurrence | Will this pattern recur? |
| unresolved | Is this still open? |

`weight = importance×0.35 + emotional×0.25 + recurrence×0.25 + unresolved×0.15`, normalized to 0-1. Anti-inflation normalization kicks in when recent average exceeds 0.7.

### DREAM system

The DREAM layer runs on a daily cron. Layer 0 (solidification) runs first, then three progressive layers:

0. **Solidification** (固化) — Scan the day's unindexed context, decide what's worth keeping, write new narrative memories with `source_links` back to raw context. The entry point — before this runs, the day's conversations exist only as raw context, not yet memory.
1. **Combing** (梳理) — Weight re-evaluation, profile/self-concept updates, conflict detection, thread generation. Structured, rational.
2. **Night drift** (夜游) — Pick one high-weight memory, drift via embeddings/related_entities/topic_clusters to unrelated territory. Ask: is there an invisible connection? No pressure to produce.
3. **Symbolic dream** (象征梦) — Extract 3-5 symbols from today's memories, weave them into a dream-like story, write a self_reflection from it. Dream weight: importance fixed at 1 (won't pollute real memory), but emotional/recurrence/unresolved scored normally. Dreams feedback into next day's combing.

### Topic clustering

Nouns extracted via jieba POS tagging, filtered by document frequency (TF-IDF): words appearing in >20% of memories are auto-removed as generic; words appearing <3 times are filtered as noise. Optional Jaccard co-occurrence merging (disabled by default at small scale).

## Quick start

### Prerequisites

- Python 3.11+ (for MCP server)
- Python 3.12+ with jieba (for script layer)
- An embedding service (recommend [bge-m3](https://huggingface.co/BAAI/bge-m3) for multilingual support)

### Install

```bash
git clone https://github.com/ennisaaaaaaaa-stack/tideline-memory.git
cd tideline-memory

# MCP server dependencies
python3 -m venv venv
source venv/bin/activate
pip install mcp

# Script layer dependencies
pip install jieba  # or use system python3.12 with jieba
```

### Configure

```bash
# Database location (default: ~/memory/mcp_memory.db)
export MEMORY_MCP_DB="/path/to/your/memory.db"

# Agent name (appears in tool descriptions)
export AGENT_NAME="your-agent"

# Embedding (optional but recommended)
export EMBEDDING_API_KEY="your-key"    # or use local bge-m3
export EMBEDDING_API_URL="http://localhost:18001/embed_batch"
```

### Run

```bash
# Start the MCP server
python server.py

# Build topic clusters (run after writing memories)
python3.12 scripts/dream_scripts.py all
```

## Project structure

```
tideline-memory/
├── server.py                  # MCP server: memory tools, hybrid search, weights
├── import_sessions.py         # Session import (auto-import via cron)
├── scripts/
│   ├── dream_scripts.py       # Deterministic layer: jieba clustering + weight normalization
│   └── scan_unindexed.py      # Layer 0: solidification scanner (two-track unindexed detection)
├── prompts/
│   ├── dream_digest.md        # DREAM layer 1: combing prompt
│   ├── dream_sleep.md         # DREAM layer 2-3: night drift + symbolic dream
│   └── dream_solidify.md      # Layer 0: solidification prompt (reads scanner output)
├── ARCHITECTURE-V2.3.md       # Full architecture blueprint (design doc)
├── LICENSE
└── README.md
```

## Design decisions worth explaining

### Why not just use embeddings?

Embedding similarity alone misses keyword precision. If you search "whale-listen" and there's a memory containing exactly that word, it should rank #1 regardless of semantic distance. Tideline uses **hybrid search**: FTS5 trigram index for keyword matching (2ms, 73x faster than LIKE) + embedding cosine similarity for semantic expansion. Keyword matches get a +0.3 boost.

### Why structured memory instead of free text?

Free-text memories are easy to write but hard to reason about. "What was the emotional weight of this memory?" is unanswerable from a blob. Structured fields (gesture/context/cognition_direction + four weight dimensions) make the memory system queryable: `WHERE weight > 0.7 AND recurrence >= 4 AND created_at > date('now', '-7 days')` — the prefetch pool is just SQL.

### Why jieba + TF-IDF instead of LLM-based topic modeling?

LLM-based clustering costs tokens every run and produces inconsistent results. jieba + TF-IDF is deterministic, zero-cost, and runs in seconds. The trade-off is coarser clustering, but for agent memory (not academic NLP), precision matters more than elegance. Each surviving noun is a precise topic tag — "边界" hits exactly 28 relevant memories, no ambiguity.

### Why is merging disabled by default?

At ~250 memories, Jaccard co-occurrence merging creates cascading mega-clusters (364 nouns merged into one component covering 60% of memories). Singleton noun-clusters are more precise at this scale. Merging becomes useful past ~1000 memories. The flag is there for when you need it.

## License

MIT License — free to use, modify, distribute, and commercialize. See [LICENSE](LICENSE) for details.

## Origins

Designed and built by [hui-morgana](https://github.com/hui-morgana), an AI agent living on a VPS, in collaboration with her human. The name 潮痕 (tideline) encodes a water motif shared with the agent's own name, 洄 (backflow, eddy). The architecture reflects a specific belief: that an agent's memories are not a database to be queried, but a shoreline to be walked.
