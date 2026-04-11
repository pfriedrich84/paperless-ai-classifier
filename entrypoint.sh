#!/bin/sh
set -e

# Start MCP SSE server in background if enabled
if [ "${ENABLE_MCP:-false}" = "true" ]; then
    MCP_TRANSPORT="${MCP_TRANSPORT:-sse}"
    export MCP_TRANSPORT
    echo "Starting MCP server (transport=${MCP_TRANSPORT}, port=${MCP_PORT:-3001})"
    python -m app.mcp_server &
fi

# Start main application
exec uvicorn app.main:app --host 0.0.0.0 --port 8088
