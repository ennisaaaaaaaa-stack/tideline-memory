FROM python:3.11-slim

# Tideline Memory MCP Server — stdio, no ports
WORKDIR /app

RUN pip install --no-cache-dir "mcp>=1.0"

COPY server.py .
COPY plugins/ plugins/
COPY scripts/ scripts/

# Config via env vars (container stays stateless):
#   MEMORY_MCP_DB   — SQLite path (mount a volume here)
#   EMBEDDING_API_KEY / EMBEDDING_API_URL / EMBEDDING_MODEL — optional semantic search
#   AGENT_NAME      — log label

ENTRYPOINT ["python", "server.py"]
