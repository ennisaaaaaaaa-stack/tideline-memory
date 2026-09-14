#!/usr/bin/env bash
# Tideline DREAM Orchestrator — deterministic layer
#
# Runs the zero-token-cost pipeline:
#   1. Embedding service health check (wait until ready)
#   2. dream_scripts.py all (jieba clusters + weight backfill + recurrence + normalization)
#   3. soft_clusters.py build (k-means soft clustering + adjacency matrix)
#   4. scan_unindexed.py (Layer 0 scanner — feeds into solidification LLM)
#   5. build_entity_graph.py (entity graph from entities_role — full rebuild)
#
# Safe to run anytime. Zero LLM cost. Idempotent.
# The LLM layers (solidification → digest → sleep) are Hermes cron jobs
# scheduled AFTER this script completes.

set -euo pipefail

# ── Config ────────────────────────────────────────────────
export MEMORY_MCP_DB="${MEMORY_MCP_DB:-/root/memory/mcp_memory.db}"
export EMBEDDING_API_URL="${EMBEDDING_API_URL:-http://127.0.0.1:8800/embed_batch}"
TIDELINE_DIR="/root/tideline-memory"
PYTHON="${TIDELINE_DIR}/venv/bin/python3"
LOG_FILE="/root/tideline-memory/logs/orchestrator.log"
MAX_EMBED_WAIT=120  # seconds to wait for embedding service

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

# ── Phase 0: Health checks ────────────────────────────────
log "═══ Tideline DREAM Orchestrator — start ═══"

# 0a. Embedding service health check
log "Phase 0: Waiting for embedding service at $EMBEDDING_API_URL ..."
waited=0
while [ $waited -lt $MAX_EMBED_WAIT ]; do
    if curl -sf --noproxy '*' --max-time 5 \
       -X POST "$EMBEDDING_API_URL" \
       -H "Content-Type: application/json" \
       -d '{"texts":["health check"]}' > /dev/null 2>&1; then
        log "  ✅ Embedding service ready (waited ${waited}s)"
        break
    fi
    sleep 3
    waited=$((waited + 3))
done

if [ $waited -ge $MAX_EMBED_WAIT ]; then
    log "  ❌ Embedding service not available after ${MAX_EMBED_WAIT}s — aborting"
    exit 1
fi

# 0b. Database exists
if [ ! -f "$MEMORY_MCP_DB" ]; then
    log "  ❌ Database not found: $MEMORY_MCP_DB — aborting"
    exit 1
fi
log "  ✅ Database: $MEMORY_MCP_DB"

# ── Phase 1: Script layer (deterministic, zero token) ────
log "Phase 1: dream_scripts.py (clusters + weights + recurrence + normalization)"
cd "$TIDELINE_DIR"
$PYTHON scripts/dream_scripts.py all 2>&1 | tee -a "$LOG_FILE"
log "  ✅ dream_scripts complete"

# ── Phase 2: Soft clustering ──────────────────────────────
log "Phase 2: soft_clusters.py (k-means + adjacency matrix)"
$PYTHON scripts/soft_clusters.py build 2>&1 | tee -a "$LOG_FILE"
log "  ✅ soft_clusters complete"

# ── Phase 3: Layer 0 scanner (feeds solidification LLM) ───
log "Phase 3: scan_unindexed.py (unindexed context detection)"
$PYTHON scripts/scan_unindexed.py > /tmp/scan_unindexed_output.md 2>&1
SCAN_LINES=$(wc -l < /tmp/scan_unindexed_output.md)
log "  ✅ scan_unindexed complete ($SCAN_LINES lines of output)"
log "  Output saved to /tmp/scan_unindexed_output.md (for solidification cron)"

# ── Phase 4: Attention tracker init (idempotent) ─────────
# attention_tracker.py was removed in review-6; tables are now created
# by attention_shared.py on first log_attention() call.
log "Phase 4: Attention tables (via attention_shared.py — lazy init on first use)"
log "  ✅ Attention tables ready (lazy init)"

# ── Phase 5: Entity graph (deterministic, full rebuild) ───
# Was a manual orphan since 2026-08-09 pipeline launch — graph data froze
# at 8/27 while narratives kept growing (spoor 大戏 zero-in-graph, dream
# night-73). Added to rotation night-74 (2026-09-15): rebuild every run.
log "Phase 5: build_entity_graph.py (entity graph from entities_role)"
$PYTHON scripts/build_entity_graph.py "$MEMORY_MCP_DB" 2>&1 | tee -a "$LOG_FILE"
log "  ✅ entity graph complete"

# ── Done ──────────────────────────────────────────────────
log "═══ Orchestrator deterministic layer complete ═══"
log "Next: LLM layers via Hermes cron jobs:"
log "  Layer 0 (固化): scan_unindexed output → dream_solidify.md prompt"
log "  Layer 1 (梳理): dream_digest.md prompt"
log "  Layer 2-3 (做梦): dream_sleep.md prompt"
log ""
