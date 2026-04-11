# paperless-ai-classifier

KI-basierter Klassifikator für [Paperless-NGX](https://docs.paperless-ngx.com/), der neu eingescannte Dokumente (Tag `Posteingang`) automatisch verprobt und Vorschläge für **Titel, Datum, Korrespondent, Dokumenttyp und Speicherpfad** erzeugt. Läuft als **ein einzelner Docker-Container** gegen eine lokale **Ollama**-Instanz.

Alle Vorschläge landen in einer Review-Queue und werden erst nach manueller Freigabe in Paperless geschrieben. Neue Tags, die das LLM vorschlägt, werden nur angelegt, wenn du sie in der Tag-Whitelist freigibst.

## Features

- 🔍 Polling von Paperless-NGX nach Dokumenten mit Tag `Posteingang`
- 🧠 Klassifikation via Ollama (Default: `gemma3:4b`, konfigurierbar)
- 📚 Kontextaware durch Embedding-Similarity-Search über bereits klassifizierte Dokumente (`sqlite-vec`)
- ✅ Review-GUI mit HTMX: Annehmen / Ablehnen / Editieren in einem Klick
- 🏷️ Tag-Whitelist: Neue Tags werden vorgeschlagen, aber erst nach Freigabe in Paperless angelegt
- 📝 Optionaler OCR-Correction-Pass (nur wenn OCR erkennbar kaputt ist)
- 🗄️ SQLite-State mit vollständigem Audit-Trail
- 🔁 Idempotent: verarbeitet jedes Dokument nur einmal
- 🤖 Telegram-Bot: Vorschläge direkt im Chat annehmen/ablehnen (optional)
- 🔌 MCP Server: Paperless-NGX + KI-Klassifikation als Tools für Claude Code und andere KI-Assistenten (optional)
- 🐳 Single-Container, Dockhand-ready, GitHub Actions für Image-Build

## Architektur

```
┌────────────────┐   poll     ┌──────────────────────┐
│ Paperless-NGX  │◀───────────│  Worker (APScheduler)│
│  (Tag: Post-   │            │   - fetch inbox docs │
│   eingang)     │──docs─────▶│   - build context    │
└────────────────┘            │   - call Ollama      │
        ▲                     │   - store suggestion │
        │                     └──────────┬───────────┘
        │ PATCH                          │
        │ (nach Freigabe)                ▼
        │                     ┌──────────────────────┐
        │                     │   SQLite + vec0      │
        │                     │   - suggestions      │
        │                     │   - tag whitelist    │
        │                     │   - embeddings       │
        │                     │   - audit log        │
        │                     └──────────┬───────────┘
        │                                │
        │                                ▼
        │                     ┌──────────────────────┐
        └─────────────────────│  FastAPI + HTMX GUI  │
                              │   - /review          │
                              │   - /tags            │
                              │   - /stats           │
                              │   - /errors          │
                              └──────────────────────┘
                                         ▲
                                         │
                                       Browser
```

## Quickstart

```bash
# 1. Repo klonen
git clone git@github.com:pfriedrich84/paperless-ai-classifier.git
cd paperless-ai-classifier

# 2. .env anlegen
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 3. Ollama-Modell ziehen (auf dem Ollama-Host)
ollama pull gemma3:4b

# 4. Starten
docker compose up -d --build

# 5. GUI öffnen
open http://localhost:8088
```

## Konfiguration

Alle Einstellungen laufen über `.env`. Siehe `.env.example` für die vollständige Liste.

### Wichtigste Variablen

| Variable | Default | Beschreibung |
|---|---|---|
| `PAPERLESS_URL` | — | Basis-URL, z.B. `http://paperless:8000` |
| `PAPERLESS_TOKEN` | — | API-Token (Paperless → Admin → Tokens) |
| `PAPERLESS_INBOX_TAG_ID` | — | ID des Tags `Posteingang` |
| `PAPERLESS_PROCESSED_TAG_ID` | — | Optional: Tag, der nach Commit gesetzt wird |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama-Endpoint |
| `OLLAMA_MODEL` | `gemma3:4b` | Klassifikations-Modell |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding-Modell für Kontext |
| `POLL_INTERVAL_SECONDS` | `300` | Wie oft die Inbox gepollt wird |
| `CONTEXT_MAX_DOCS` | `5` | Wieviele ähnliche Dokumente in den Prompt |
| `AUTO_COMMIT_CONFIDENCE` | `0` | Wenn > 0: ab diesem Score automatisch committen (0–100) |
| `GUI_PORT` | `8088` | Port der Review-GUI |
| `ENABLE_TELEGRAM` | `false` | Telegram-Bot aktivieren |
| `TELEGRAM_BOT_TOKEN` | — | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat-ID für Benachrichtigungen |
| `MCP_TRANSPORT` | `stdio` | MCP-Transport: `stdio`, `sse`, `streamable-http` |
| `MCP_ENABLE_WRITE` | `false` | MCP-Write-Tools aktivieren |
| `MCP_API_KEY` | — | MCP-Auth (empfohlen bei SSE) |

## Review-Workflow

1. Ein neues Dokument wird in Paperless hochgeladen, bekommt den Tag `Posteingang`.
2. Worker erkennt es beim nächsten Poll, zieht Volltext + Metadaten.
3. Context Builder sucht per Embedding-Similarity die ähnlichsten bereits klassifizierten Dokumente.
4. Ollama bekommt System-Prompt, Kontext und den neuen Dokumenttext, liefert strukturiertes JSON.
5. Eintrag landet in `suggestions` mit Status `pending`.
6. In der GUI: durchklicken, editieren, freigeben. Tags, die noch nicht in der Whitelist sind, werden dabei **staged** und müssen separat unter `/tags` freigegeben werden, bevor sie in Paperless angelegt werden.
7. Nach Commit: Felder werden via PATCH gegen Paperless geschrieben, `Posteingang` entfernt, optional `Processed` gesetzt.

## MCP Server (optional)

[Model Context Protocol](https://modelcontextprotocol.io/) Server, der Paperless-NGX und die KI-Klassifikation als Tools für KI-Assistenten (Claude Code, etc.) bereitstellt.

```bash
# stdio (für Claude Code / lokale Nutzung)
python -m app.mcp_server

# SSE (für HTTP-Clients)
MCP_TRANSPORT=sse MCP_PORT=3001 python -m app.mcp_server

# Docker
docker compose -f docker-compose.yml -f docker-compose.mcp.yml up
```

### Sicherheitskonzept

- **Read-Only als Default.** Schreibende Tools (`update_document`, `approve_suggestion`, `reject_suggestion`, `approve_tag`) nur bei `MCP_ENABLE_WRITE=true`.
- **API-Key-Auth:** `MCP_API_KEY` sichert alle Tool-Calls ab (empfohlen bei SSE-Transport).
- **Rate-Limit:** `MCP_CLASSIFY_RATE_LIMIT=10` begrenzt KI-Klassifikationen auf 10 pro Stunde.
- **Inbox-Gate:** `classify_document` akzeptiert nur Dokumente mit dem Inbox-Tag.

### Verfügbare Tools

| Kategorie | Tools | Modus |
|---|---|---|
| Dokumente | `search_documents`, `get_document`, `list_inbox` | read-only |
| Dokumente | `update_document` | write (opt-in) |
| Entities | `list_correspondents`, `list_document_types`, `list_tags`, `list_storage_paths` | read-only |
| KI | `classify_document` (rate-limited), `find_similar_documents` | read-only |
| Suggestions | `list_suggestions`, `get_suggestion` | read-only |
| Suggestions | `approve_suggestion`, `reject_suggestion` | write (opt-in) |
| Tags | `list_tag_proposals` | read-only |
| Tags | `approve_tag` | write (opt-in) |
| System | `get_status` | read-only |

### MCP-Konfiguration

| Variable | Default | Beschreibung |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | Transport: `stdio`, `sse`, `streamable-http` |
| `MCP_PORT` | `3001` | Port für SSE/HTTP-Transport |
| `MCP_HOST` | `0.0.0.0` | Bind-Adresse |
| `MCP_ENABLE_WRITE` | `false` | Write-Tools aktivieren |
| `MCP_API_KEY` | — | API-Key für Authentifizierung |
| `MCP_CLASSIFY_RATE_LIMIT` | `10` | Max. Klassifikationen pro Stunde (0 = unbegrenzt) |

### Claude Code Integration

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

## Deployment via Dockhand

Siehe [`CLAUDE.md`](./CLAUDE.md) für Details. Kurzform:

1. Privates GitHub-Repo anlegen, Code pushen
2. Deploy-Key in GitHub hinterlegen
3. Dockhand → Stacks → Create from Git
4. `.env` als External Env File auf dem Docker-Host bereitstellen (`/opt/stacks/paperless-ai-classifier/.env`)
5. Auto-Sync aktivieren oder Webhook einrichten

## Lizenz

MIT — siehe `LICENSE`.
