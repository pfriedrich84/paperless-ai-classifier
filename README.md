# paperless-ai-classifier

[![Built with Claude Code](https://img.shields.io/badge/Built%20with-Claude%20Code-blueviolet?logo=claude&logoColor=white)](https://claude.ai/code)

KI-basierter Klassifikator für [Paperless-NGX](https://docs.paperless-ngx.com/), der neu eingescannte Dokumente (Tag `Posteingang`) automatisch verprobt und Vorschläge für **Titel, Datum, Korrespondent, Dokumenttyp und Speicherpfad** erzeugt. Läuft als **ein einzelner Docker-Container** gegen eine lokale **Ollama**-Instanz.

Alle Vorschläge landen in einer Review-Queue und werden erst nach manueller Freigabe in Paperless geschrieben. Neue Tags, die das LLM vorschlägt, werden nur angelegt, wenn du sie in der Tag-Whitelist freigibst.

## Features

- 🔍 Polling von Paperless-NGX nach Dokumenten mit Tag `Posteingang`
- 🧠 Klassifikation via Ollama (Default: `gemma4:e2b`, konfigurierbar)
- 📚 Kontextaware durch Embedding-Similarity-Search über bereits klassifizierte Dokumente (`sqlite-vec`) — Kontext-Dokumente liefern ihre vollständige Klassifikation (Korrespondent, Typ, Tags, Speicherpfad) als Referenz
- ✅ Review-GUI mit HTMX: Annehmen / Ablehnen / Editieren in einem Klick
- 🏷️ Tag-Whitelist: Neue Tags werden vorgeschlagen, aber erst nach Freigabe in Paperless angelegt
- 📝 Multi-Level OCR-Korrektur: text-only, vision-light oder vision-full (konfigurierbar via `OCR_MODE`)
- 🗄️ SQLite-State mit vollständigem Audit-Trail
- 🔁 Idempotent: verarbeitet jedes Dokument nur einmal
- 💬 RAG Chat: Fragen zu deinen Dokumenten stellen — im Browser (`/chat`) oder direkt im Telegram-Chat
- 🤖 Telegram-Bot: Vorschläge annehmen/ablehnen + RAG-Chat für Dokument-Fragen (optional)
- 🔌 MCP Server: Paperless-NGX + KI-Klassifikation als Tools für Claude Code und andere KI-Assistenten (optional)
- 🚀 Setup-Wizard: Geführtes Onboarding mit Verbindungstests beim ersten Start (`/setup`)
- 📊 Embeddings-Dashboard: Vektor-DB-Inspektion und Similarity-Search (`/embeddings`)
- 📥 Inbox-View: Posteingang mit Dokumenten-Karten und Bulk-Aktionen (`/inbox`)
- 🏷️ Tag-Blacklist: Unerwünschte Tags dauerhaft unterdrücken (`/tags`)
- 🔔 Webhook-Support: Sofortige Verarbeitung als Alternative/Ergänzung zum Polling
- ⚙️ Settings UI: Konfiguration im Browser ändern, ohne Container-Neustart (`/settings`)
- 🐳 Single-Container, Dockhand-ready, fertiges Image via [GitHub Container Registry](https://ghcr.io/pfriedrich84/paperless-ai-classifier)

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
                              │   - /chat            │
                              │   - /inbox           │
                              │   - /tags            │
                              │   - /embeddings      │
                              │   - /stats           │
                              │   - /settings        │
                              │   - /setup           │
                              │   - /errors          │
                              └──────────────────────┘
                                         ▲
                                         │
                                       Browser
```

## Quickstart

### Option A: Fertiges Image von GHCR (empfohlen)

```bash
# 1. docker-compose.yml und .env herunterladen
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/.env.example
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 2. Ollama-Modelle ziehen (auf dem Ollama-Host)
ollama pull gemma4:e2b
ollama pull nomic-embed-text-v2-moe

# 3. Starten (zieht automatisch ghcr.io/pfriedrich84/paperless-ai-classifier:latest)
docker compose up -d

# 4. GUI öffnen
open http://localhost:8088
```

> **Verfügbare Tags:**
> - `latest` — aktueller Stand von `main`
> - `v0.1.0`, `v0.1` — versionierte Releases (bei getaggten Releases)
> - `sha-<hash>` — spezifischer Commit

### Option B: Selbst bauen

```bash
# 1. Repo klonen
git clone git@github.com:pfriedrich84/paperless-ai-classifier.git
cd paperless-ai-classifier

# 2. .env anlegen
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 3. Ollama-Modelle ziehen (auf dem Ollama-Host)
ollama pull gemma4:e2b
ollama pull nomic-embed-text-v2-moe

# 4. Bauen und starten
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
| `KEEP_INBOX_TAG` | `true` | Posteingang-Tag nach Commit beibehalten |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama-Endpoint |
| `OLLAMA_MODEL` | `gemma4:e2b` | Klassifikations-Modell |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text-v2-moe` | Embedding-Modell für Kontext (multilingual) |
| `OLLAMA_EMBED_RETRIES` | `3` | Anzahl Retries bei Embedding-Fehlern (Truncation + transiente 500er) |
| `OLLAMA_EMBED_RETRY_BASE_DELAY` | `1.0` | Basis-Delay in Sekunden für exponentiellen Backoff |
| `OLLAMA_NUM_CTX` | `8192` | Kontextfenster für das Chat-Modell (Tokens) |
| `OLLAMA_EMBED_NUM_CTX` | `512` | Kontextfenster für das Embedding-Modell (Tokens) |
| `POLL_INTERVAL_SECONDS` | `300` | Wie oft die Inbox gepollt wird |
| `CONTEXT_MAX_DOCS` | `5` | Wieviele ähnliche Dokumente in den Prompt |
| `AUTO_COMMIT_CONFIDENCE` | `0` | Wenn > 0: ab diesem Score automatisch committen (0–100) |
| `GUI_PORT` | `8088` | Port der Review-GUI |
| `ENABLE_TELEGRAM` | `false` | Telegram-Bot aktivieren |
| `TELEGRAM_BOT_TOKEN` | — | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | — | Chat-ID für Benachrichtigungen |
| `ENABLE_MCP` | `false` | MCP-Server im selben Container mitlaufen lassen |
| `MCP_TRANSPORT` | `sse` | MCP-Transport: `sse`, `streamable-http` (stdio nur lokal) |
| `MCP_ENABLE_WRITE` | `false` | MCP-Write-Tools aktivieren |
| `MCP_API_KEY` | — | MCP-Auth (empfohlen bei SSE) |
| `OLLAMA_OCR_MODEL` | `gemma3:1b` | Kleineres Modell für Text-Only OCR-Korrektur |
| `OCR_MODE` | `off` | OCR-Stufe: `off`, `text`, `vision_light`, `vision_full` |
| `OCR_VISION_MODEL` | *(= OLLAMA_MODEL)* | Vision-Modell für OCR (muss vision-fähig sein) |
| `OCR_VISION_MAX_PAGES` | `3` | Max. Seiten für Vision-OCR |
| `OCR_VISION_DPI` | `150` | Render-Auflösung für PDF-Seiten |
| `WEBHOOK_SECRET` | — | Shared Secret für `POST /webhook/paperless` |

## Review-Workflow

1. Ein neues Dokument wird in Paperless hochgeladen, bekommt den Tag `Posteingang`.
2. Worker erkennt es beim nächsten Poll, zieht Volltext + Metadaten.
3. Context Builder sucht per Embedding-Similarity die ähnlichsten bereits klassifizierten Dokumente (Inbox-Dokumente werden ausgeschlossen — nur reviewte/bestätigte Docs dienen als Kontext).
4. Ollama bekommt System-Prompt, Kontext-Dokumente **mit deren Klassifikation** (Korrespondent, Dokumenttyp, Speicherpfad, Tags) und den neuen Dokumenttext, liefert strukturiertes JSON.
5. Eintrag landet in `suggestions` mit Status `pending`.
6. In der GUI: durchklicken, editieren, freigeben. Tags, die noch nicht in der Whitelist sind, werden dabei **staged** und müssen separat unter `/tags` freigegeben werden, bevor sie in Paperless angelegt werden.
7. Nach Commit: Felder werden via PATCH gegen Paperless geschrieben, optional `Processed` gesetzt. Der `Posteingang`-Tag bleibt standardmaessig erhalten (`KEEP_INBOX_TAG=true`); mit `KEEP_INBOX_TAG=false` wird er entfernt.

## MCP Server (optional)

[Model Context Protocol](https://modelcontextprotocol.io/) Server, der Paperless-NGX und die KI-Klassifikation als Tools für KI-Assistenten (Claude Code, etc.) bereitstellt.

```bash
# Docker: MCP läuft im selben Container mit
# In .env setzen:
#   ENABLE_MCP=true
#   MCP_API_KEY=ein-sicherer-key
docker compose up -d

# Lokal (stdio, für Claude Code CLI):
python -m app.mcp_server

# Lokal (SSE):
MCP_TRANSPORT=sse MCP_PORT=3001 python -m app.mcp_server
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
| Tags | `list_tag_proposals`, `list_blacklisted_tags` | read-only |
| Tags | `approve_tag`, `unblacklist_tag` | write (opt-in) |
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

---

Developed & maintained by [@pfriedrich84](https://github.com/pfriedrich84), AI-assisted with [Claude Code](https://claude.ai/code).
