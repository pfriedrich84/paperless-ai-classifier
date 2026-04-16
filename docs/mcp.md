# MCP Server

[Model Context Protocol](https://modelcontextprotocol.io/) Server, der Paperless-NGX
und die KI-Klassifikation als Tools fuer KI-Assistenten (Claude Code, etc.) bereitstellt.

Der MCP-Server laeuft optional im selben Container wie die Haupt-App.

## Aktivierung

```bash
# In .env setzen:
ENABLE_MCP=true
MCP_API_KEY=ein-sicherer-key    # empfohlen bei SSE-Transport

# Container starten
docker compose up -d
```

### Lokaler Start (Entwicklung)

```bash
# stdio (fuer Claude Code CLI):
python -m app.mcp_server

# SSE:
MCP_TRANSPORT=sse MCP_PORT=3001 python -m app.mcp_server
```

## Sicherheitskonzept

- **Read-Only als Default.** Schreibende Tools nur bei `MCP_ENABLE_WRITE=true`.
- **API-Key-Auth:** `MCP_API_KEY` sichert alle Tool-Calls ab (empfohlen bei SSE-Transport).
- **Rate-Limit:** `MCP_CLASSIFY_RATE_LIMIT` begrenzt KI-Klassifikationen pro Stunde (Default: 10).
- **Inbox-Gate:** `classify_document` akzeptiert nur Dokumente mit dem Inbox-Tag.

## Verfuegbare Tools

### Read-Only (immer verfuegbar)

| Kategorie | Tools |
|---|---|
| Dokumente | `search_documents`, `get_document`, `list_inbox` |
| Entities | `list_correspondents`, `list_document_types`, `list_tags`, `list_storage_paths` |
| KI | `classify_document` (rate-limited), `find_similar_documents` |
| Suggestions | `list_suggestions`, `get_suggestion` |
| Tags | `list_tag_proposals`, `list_blacklisted_tags` |
| System | `get_status` |

### Write (opt-in via `MCP_ENABLE_WRITE=true`)

| Kategorie | Tools |
|---|---|
| Dokumente | `update_document` |
| Suggestions | `approve_suggestion`, `reject_suggestion` |
| Tags | `approve_tag` (retroaktiv auf committete Docs), `unblacklist_tag` |

## Resources

| URI | Beschreibung |
|---|---|
| `paperless://suggestions/pending` | Offene Vorschlaege |
| `paperless://stats` | Classifier-Statistiken (Suggestions, Errors, Tags) |

## Konfiguration

| Variable | Default | Beschreibung |
|---|---|---|
| `ENABLE_MCP` | `false` | MCP-Server aktivieren |
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, `streamable-http` |
| `MCP_PORT` | `3001` | Port fuer SSE/HTTP-Transport |
| `MCP_HOST` | `0.0.0.0` | Bind-Adresse |
| `MCP_ENABLE_WRITE` | `false` | Write-Tools aktivieren |
| `MCP_API_KEY` | — | API-Key fuer Authentifizierung |
| `MCP_CLASSIFY_RATE_LIMIT` | `10` | Max. Klassifikationen pro Stunde (0 = unbegrenzt) |

## Integration Examples

Below are example configurations to connect the MCP server with client tools.

### Local CLI Usage

```json
{
  "mcpServers": {
    "paperless": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/paperless-ai-classifier",
      "env": {
        "PAPERLESS_URL": "http://localhost:8000",
        "PAPERLESS_TOKEN": "your-token",
        "PAPERLESS_INBOX_TAG_ID": "1"
      }
    }
  }
}
```

### SSE Transport (e.g., Docker remote server)

```json
{
  "mcpServers": {
    "paperless": {
      "type": "sse",
      "url": "http://classifier-host:3001/sse",
      "headers": {
        "X-API-Key": "your-mcp-api-key"
      }
    }
  }
}
```
