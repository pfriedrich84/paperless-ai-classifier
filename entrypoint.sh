#!/bin/sh
set -e

# Start Meilisearch in background (hybrid search sidecar)
MEILI_DB_PATH="${DATA_DIR:-/data}/meili_data"
mkdir -p "$MEILI_DB_PATH"
echo "Starting Meilisearch (data=$MEILI_DB_PATH)"
meilisearch \
    --db-path "$MEILI_DB_PATH" \
    --http-addr "127.0.0.1:7700" \
    --master-key "${MEILISEARCH_API_KEY:-}" \
    --no-analytics &

# Wait for Meilisearch to be ready (max 10s)
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf http://localhost:7700/health > /dev/null 2>&1; then
        echo "Meilisearch ready"
        break
    fi
    if [ "$i" = "10" ]; then
        echo "WARNING: Meilisearch not ready after 10s — continuing anyway"
    fi
    sleep 1
done

# Start MCP SSE server in background if enabled
if [ "${ENABLE_MCP:-false}" = "true" ]; then
    MCP_TRANSPORT="${MCP_TRANSPORT:-sse}"
    export MCP_TRANSPORT
    echo "Starting MCP server (transport=${MCP_TRANSPORT}, port=${MCP_PORT:-3001})"
    python -m app.mcp_server &
fi

# Start main application
exec uvicorn app.main:app --host 0.0.0.0 --port 8088
