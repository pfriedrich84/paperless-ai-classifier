# ArchiBot

[![AI Assisted](https://img.shields.io/badge/AI-Assisted-blueviolet)](https://github.com/pfriedrich84/paperless-ai-classifier)

<p align="center">
  <img src="app/static/logo-full.png" alt="ArchiBot Logo" width="256">
</p>

KI-basierter Klassifikator für [Paperless-NGX](https://docs.paperless-ngx.com/), der neu eingescannte Dokumente (Tag `Posteingang`) automatisch verprobt und Vorschläge für **Titel, Datum, Korrespondent, Dokumenttyp und Speicherpfad** erzeugt. Läuft als **ein einzelner Docker-Container** gegen eine lokale **Ollama**-Instanz.

Alle Vorschläge landen in einer Review-Queue und werden erst nach manueller Freigabe in Paperless geschrieben. Neue Tags, die das LLM vorschlägt, werden nur angelegt, wenn du sie in der Tag-Whitelist freigibst.

## Features

- 🔍 Polling von Paperless-NGX nach Dokumenten mit Tag `Posteingang`
- 🧠 Klassifikation via Ollama (Default: `gemma4:26b-a4b-it-q4_K_M`, konfigurierbar)
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
- 🔔 Webhook-Support: Sofortige Verarbeitung + Embedding-Update via Paperless-Workflow-Webhooks
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

```bash
# 1. docker-compose.yml und .env herunterladen
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/.env.example
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 2. Ollama-Modelle ziehen (auf dem Ollama-Host)
ollama pull gemma4:26b-a4b-it-q4_K_M
ollama pull qwen3-embedding:0.6b
ollama pull qwen3:0.6b            # OCR-Korrektur (optional)
ollama pull qwen3-vl:2b           # Vision-OCR (optional)

# 3. Starten
docker compose up -d

# 4. GUI öffnen → Setup-Wizard führt durch die Ersteinrichtung
open http://localhost:8088
```

Weitere Optionen (selbst bauen, lokale Entwicklung): **[docs/installation.md](./docs/installation.md)**

## Dokumentation

| Dokument | Beschreibung |
|---|---|
| **[Installation](./docs/installation.md)** | Quickstart, Docker-Setup, lokale Entwicklung |
| **[Konfiguration](./docs/configuration.md)** | Alle Umgebungsvariablen im Detail |
| **[Review-Workflow](./docs/workflow.md)** | Klassifikation, Review, Tag-Management |
| **[CLI Commands](./docs/cli.md)** | Manuelle Pipeline-Steuerung und Container-Reset |
| **[MCP Server](./docs/mcp.md)** | KI-Tools für Claude Code und andere Assistenten |
| **[Deployment](./docs/deployment.md)** | Dockhand, Reverse Proxy, Backup |
| **[Architektur](./docs/architecture.md)** | Datenfluss-Diagramme und System-Kontext |
| **[Webhooks](./docs/webhooks.md)** | Sofortige Verarbeitung statt Polling |

## Lizenz

MIT — siehe `LICENSE`.

---

Developed & maintained by [@pfriedrich84](https://github.com/pfriedrich84), AI‑assisted.
