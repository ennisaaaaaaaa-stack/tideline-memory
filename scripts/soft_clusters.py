#!/usr/bin/env python3
"""
Tideline v2.4 — Soft Clustering (k-means in embedding space)

Replaces/augments jieba noun-frequency clustering with embedding-space
k-means. Key differences from existing jieba clustering:

1. Model-agnostic: works with ANY embedding model (bge-m3, OpenAI, etc.)
2. Soft assignment: each narrative belongs to top-K nearest centroids
   (not just one cluster). Cross-topic memories get multi-cluster visibility.
3. Adjacency matrix: clusters that share narratives are "adjacent".
   Enables query routing: hit cluster A → spread to adjacent clusters.
4. Dynamic k: scales with data size. k = max(5, int(sqrt(N) * 1.5))

Tables:
- emb_clusters: cluster definitions (id, name, centroid_vector, member_count)
- emb_cluster_members: soft membership (narrative_id, cluster_id, distance)
- emb_cluster_adjacency: inter-cluster connection strengths

Usage:
  python3 soft_clusters.py init          # create tables
  python3 soft_clusters.py build         # run k-means + adjacency
  python3 soft_clusters.py route <query> # test query routing
  python3 soft_clusters.py report        # show cluster summary

Runs independently. Safe to run anytime. Zero LLM cost.
Requires: numpy (for vectorized cosine + k-means)
"""

import os, sys, json, sqlite3, math
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

try:
    import numpy as np
except ImportError:
    print("ERROR: numpy required. Install: pip install numpy", file=sys.stderr)
    sys.exit(1)

DB_PATH = os.environ.get("MEMORY_MCP_DB", str(Path.home() / "memory" / "mcp_memory.db"))

def _now():
    return datetime.now(timezone.utc).isoformat()

def _db():
    """Safe DB connection with WAL + busy timeout."""
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def init_tables():
    c = _db()
    c.execute("""
        CREATE TABLE IF NOT EXISTS emb_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            centroid TEXT NOT NULL,
            member_count INTEGER DEFAULT 0,
            total_hits INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS emb_cluster_members (
            narrative_id INTEGER NOT NULL,
            cluster_id INTEGER NOT NULL,
            distance REAL NOT NULL,
            PRIMARY KEY (narrative_id, cluster_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS emb_cluster_adjacency (
            cluster_a INTEGER NOT NULL,
            cluster_b INTEGER NOT NULL,
            shared_count INTEGER NOT NULL,
            PRIMARY KEY (cluster_a, cluster_b)
        )
    """)
    c.commit()
    c.close()
    print("Soft clustering tables ready.")

def _load_embeddings(c):
    """Load all narratives with embeddings. Returns (ids, embeddings_array)."""
    rows = c.execute(
        "SELECT id, embedding, gesture FROM narratives WHERE embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    
    ids = []
    vectors = []
    for r in rows:
        try:
            emb = json.loads(r["embedding"])
            if len(emb) > 0:
                ids.append(r["id"])
                vectors.append(emb)
        except (json.JSONDecodeError, TypeError):
            continue
    
    if not vectors:
        return [], None
    
    return ids, np.array(vectors, dtype=np.float32)

def _cosine_matrix(queries, centroids):
    """Batch cosine similarity. queries: (N, D), centroids: (K, D) → (N, K)"""
    # Normalize
    q_norm = queries / (np.linalg.norm(queries, axis=1, keepdims=True) + 1e-8)
    c_norm = centroids / (np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-8)
    return q_norm @ c_norm.T  # (N, K)

def _kmeans(vectors, k, max_iter=100, seed=42):
    """Simple k-means clustering. Returns (centroids, assignments)."""
    n, d = vectors.shape
    k = min(k, n)
    
    rng = np.random.RandomState(seed)
    # k-means++ initialization for better convergence
    centroids = [vectors[rng.randint(n)]]
    for _ in range(k - 1):
        sims = _cosine_matrix(vectors, np.array(centroids))
        # Distance = 1 - max_similarity (clipped to [0, 2])
        dists = np.clip(1 - sims.max(axis=1), 0, None)
        # Weighted random selection by distance
        probs = dists / (dists.sum() + 1e-8)
        idx = rng.choice(n, p=probs)
        centroids.append(vectors[idx])
    
    centroids = np.array(centroids)
    
    for iteration in range(max_iter):
        sims = _cosine_matrix(vectors, centroids)
        assignments = sims.argmax(axis=1)  # (N,)
        
        # Update centroids
        new_centroids = centroids.copy()
        for ci in range(k):
            mask = assignments == ci
            if mask.any():
                new_centroids[ci] = vectors[mask].mean(axis=0)
        
        # Check convergence via L2 norm of centroid shift
        shift = np.linalg.norm(new_centroids - centroids)
        centroids = new_centroids
        if shift < 1e-6:
            break
    
    # Final assignments with distances
    sims = _cosine_matrix(vectors, centroids)
    assignments = sims.argmax(axis=1)
    
    return centroids, assignments, sims

def _dynamic_k(n_items):
    """Scale k with data size: more data → more clusters."""
    k = max(5, int(math.sqrt(n_items) * 1.5))
    return min(k, n_items)

def _name_cluster(c, centroid_vec, member_ids):
    """Name a cluster by its highest-weight narrative's gesture."""
    if not member_ids:
        return f"cluster_empty"
    
    placeholders = ",".join("?" * len(member_ids))
    rows = c.execute(
        f"""SELECT id, gesture, weight FROM narratives 
            WHERE id IN ({placeholders}) ORDER BY weight DESC LIMIT 1""",
        member_ids
    ).fetchall()
    
    if not rows:
        return f"cluster_{len(member_ids)}"
    
    name = rows[0]["gesture"][:25] if rows[0]["gesture"] else f"narr_{rows[0]['id']}"
    return name

def build_clusters(top_k_assignment=3):
    """Run k-means with soft assignment + build adjacency matrix."""
    c = _db()
    ids, vectors = _load_embeddings(c)
    
    if not ids:
        print("No embeddings found.")
        c.close()
        return
    
    n = len(ids)
    k = _dynamic_k(n)
    print(f"Clustering {n} narratives into k={k} clusters (soft top-{top_k_assignment})...")
    
    # Run k-means
    centroids, hard_assignments, sims = _kmeans(vectors, k)
    print(f"K-means converged. Building soft assignments...")
    
    # Clear old data
    c.execute("DELETE FROM emb_clusters")
    c.execute("DELETE FROM emb_cluster_members")
    c.execute("DELETE FROM emb_cluster_adjacency")
    
    # Soft assignment: top-K nearest centroids per narrative
    now = _now()
    cluster_members = defaultdict(list)  # cluster_idx -> [(narrative_id, distance)]
    
    for i, nid in enumerate(ids):
        sim_row = sims[i]  # similarity to each centroid
        # Top-K clusters by similarity
        top_indices = np.argsort(sim_row)[-top_k_assignment:][::-1]
        
        for ci in top_indices:
            dist = 1.0 - sim_row[ci]  # distance = 1 - cosine_sim
            cluster_members[ci].append((nid, dist))
            c.execute(
                "INSERT OR REPLACE INTO emb_cluster_members (narrative_id, cluster_id, distance) VALUES (?,?,?)",
                (nid, ci + 1, float(dist))  # cluster_id is 1-indexed
            )
    
    # Store cluster definitions
    cluster_ids_map = {}  # old_idx -> new db id
    for ci in range(k):
        members = cluster_members.get(ci, [])
        if not members:
            continue
        
        member_ids = [m[0] for m in members]
        name = _name_cluster(c, centroids[ci], member_ids)
        centroid_json = json.dumps(centroids[ci].tolist())
        
        cursor = c.execute(
            """INSERT INTO emb_clusters (name, centroid, member_count, created_at, updated_at)
               VALUES (?,?,?,?,?)""",
            (name, centroid_json, len(members), now, now)
        )
        cluster_ids_map[ci] = cursor.lastrowid
    
    # Fix cluster_id references (old idx → new db id)
    c.execute("DELETE FROM emb_cluster_members")
    for ci, members in cluster_members.items():
        if ci not in cluster_ids_map:
            continue
        db_cid = cluster_ids_map[ci]
        for nid, dist in members:
            c.execute(
                "INSERT OR REPLACE INTO emb_cluster_members (narrative_id, cluster_id, distance) VALUES (?,?,?)",
                (nid, db_cid, float(dist))
            )
    
    # Build adjacency matrix: clusters sharing narratives are adjacent
    # narrative → set of clusters it belongs to
    narrative_to_clusters = defaultdict(set)
    for ci, members in cluster_members.items():
        if ci not in cluster_ids_map:
            continue
        db_cid = cluster_ids_map[ci]
        for nid, _ in members:
            narrative_to_clusters[nid].add(db_cid)
    
    adjacency = defaultdict(int)  # (cid_a, cid_b) → shared count
    for nid, cset in narrative_to_clusters.items():
        clist = sorted(cset)
        for i in range(len(clist)):
            for j in range(i + 1, len(clist)):
                key = (clist[i], clist[j])
                adjacency[key] += 1
    
    for (a, b), shared in adjacency.items():
        c.execute(
            "INSERT OR REPLACE INTO emb_cluster_adjacency (cluster_a, cluster_b, shared_count) VALUES (?,?,?)",
            (a, b, shared)
        )
    
    c.commit()
    
    # Stats
    total_clusters = len(cluster_ids_map)
    total_edges = len(adjacency)
    avg_members = sum(len(m) for m in cluster_members.values()) / max(total_clusters, 1)
    
    print(f"\nDone!")
    print(f"  Clusters: {total_clusters}")
    print(f"  Soft memberships: {sum(len(m) for m in cluster_members.values())}")
    print(f"  Avg members/cluster: {avg_members:.1f}")
    print(f"  Adjacency edges: {total_edges}")
    print(f"  Top adjacency:")
    
    top_edges = sorted(adjacency.items(), key=lambda x: -x[1])[:10]
    for (a, b), shared in top_edges:
        name_a = c.execute("SELECT name FROM emb_clusters WHERE id=?", (a,)).fetchone()
        name_b = c.execute("SELECT name FROM emb_clusters WHERE id=?", (b,)).fetchone()
        na = name_a["name"][:20] if name_a else f"#{a}"
        nb = name_b["name"][:20] if name_b else f"#{b}"
        print(f"    {na} ↔ {nb}: {shared} shared")
    
    c.close()

def query_route(query_text, spread=1, top_k=3):
    """Test query routing: query → nearest clusters → adjacency spread.
    
    Returns cluster IDs in order of relevance (direct hits first, then spread).
    """
    c = _db()
    
    # Embed query
    from urllib.request import Request, urlopen
    embed_url = os.environ.get("EMBEDDING_API_URL", os.environ.get("EMBED_URL", "http://localhost:8800/embed_batch"))
    data = json.dumps({"texts": [query_text[:500]]}).encode()
    req = Request(embed_url, data=data, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=10)
    result = json.loads(resp.read())
    if isinstance(result, dict) and "embeddings" in result:
        qvec = np.array(result["embeddings"][0], dtype=np.float32)
    elif isinstance(result, list):
        qvec = np.array(result[0], dtype=np.float32)
    else:
        print(f"Unexpected embed response: {str(result)[:200]}")
        return []
    
    # Load centroids
    clusters = c.execute("SELECT id, name, centroid FROM emb_clusters").fetchall()
    if not clusters:
        print("No clusters found. Run 'build' first.")
        c.close()
        return []
    
    cent_ids = [r["id"] for r in clusters]
    cent_vecs = np.array([json.loads(r["centroid"]) for r in clusters], dtype=np.float32)
    
    # Cosine similarity query → all centroids
    sims = _cosine_matrix(qvec.reshape(1, -1), cent_vecs)[0]
    
    # Top clusters
    top_idx = np.argsort(sims)[::-1][:top_k]
    print(f"Query: '{query_text[:60]}'")
    print(f"\nDirect hits:")
    hit_clusters = set()
    for i in top_idx:
        cid = cent_ids[i]
        hit_clusters.add(cid)
        print(f"  #{cid} {clusters[i]['name'][:30]:30s} sim={sims[i]:.3f}")
    
    # Adjacency spread
    if spread > 0:
        print(f"\nAdjacency spread (depth={spread}):")
        for cid in list(hit_clusters):
            neighbors = c.execute(
                """SELECT cluster_a, cluster_b, shared_count 
                   FROM emb_cluster_adjacency 
                   WHERE cluster_a=? OR cluster_b=? 
                   ORDER BY shared_count DESC LIMIT 3""",
                (cid, cid)
            ).fetchall()
            for n in neighbors:
                other = n["cluster_b"] if n["cluster_a"] == cid else n["cluster_a"]
                if other not in hit_clusters:
                    name = c.execute("SELECT name FROM emb_clusters WHERE id=?", (other,)).fetchone()
                    nname = name["name"][:30] if name else f"#{other}"
                    print(f"  → #{other} {nname:30s} (via #{cid}, shared={n['shared_count']})")
                    hit_clusters.add(other)
    
    c.close()
    return list(hit_clusters)

def report():
    """Show cluster summary."""
    c = _db()
    clusters = c.execute("""
        SELECT ec.id, ec.name, ec.member_count, ec.total_hits,
               (SELECT COUNT(*) FROM emb_cluster_adjacency 
                WHERE cluster_a=ec.id OR cluster_b=ec.id) as adjacency_count
        FROM emb_clusters ec
        ORDER BY ec.member_count DESC
    """).fetchall()
    
    if not clusters:
        print("No clusters found. Run 'build' first.")
        c.close()
        return
    
    print(f"Soft Cluster Report ({len(clusters)} clusters)\n")
    print(f"{'ID':>4} {'Members':>7} {'Adj':>4} {'Hits':>5}  Name")
    print("-" * 70)
    for r in clusters:
        name = r["name"][:40]
        print(f"{r['id']:4d} {r['member_count']:7d} {r['adjacency_count']:4d} {r['total_hits']:5d}  {name}")
    
    c.close()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    
    if cmd == "init":
        init_tables()
    elif cmd == "build":
        init_tables()
        build_clusters()
    elif cmd == "route":
        query = sys.argv[2] if len(sys.argv) > 2 else "记忆身份连续性"
        query_route(query)
    elif cmd == "report":
        report()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: init | build | route <query> | report")
