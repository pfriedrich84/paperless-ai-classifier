# Installation

Anleitung zur Einrichtung von ArchiBot — als fertiges Docker-Image
oder selbst gebaut.

## Voraussetzungen

- Docker + Docker Compose
- Eine laufende [Paperless-NGX](https://docs.paperless-ngx.com/) Instanz
- Eine laufende [Ollama](https://ollama.com/) Instanz (GPU empfohlen)
- Ollama-Modelle muessen vorab gezogen werden:
  ```bash
  ollama pull gemma4:26b-a4b-it-q4_K_M  # Klassifikation
  ollama pull qwen3-embedding:0.6b      # Embedding (1024-dim, multilingual)
  ollama pull qwen3:0.6b               # OCR-Korrektur (optional)
  ollama pull qwen3-vl:2b              # Vision-OCR (optional)
  ```

## Option A: Fertiges Image von GHCR (empfohlen)

```bash
# 1. docker-compose.yml und .env herunterladen
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/docker-compose.yml
curl -LO https://raw.githubusercontent.com/pfriedrich84/paperless-ai-classifier/main/.env.example
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 2. Starten (zieht automatisch ghcr.io/pfriedrich84/paperless-ai-classifier:latest)
docker compose up -d

# 3. GUI oeffnen
open http://localhost:8088
```

> **Verfuegbare Image-Tags:**
> - `latest` — aktueller Stand von `main`
> - `v0.1.0`, `v0.1` — versionierte Releases (bei getaggten Releases)
> - `sha-<hash>` — spezifischer Commit

## Option B: Selbst bauen

```bash
# 1. Repo klonen
git clone git@github.com:pfriedrich84/paperless-ai-classifier.git
cd paperless-ai-classifier

# 2. .env anlegen
cp .env.example .env
# → Werte eintragen (Paperless-URL, Token, Ollama-URL, Inbox-Tag-ID)

# 3. Bauen und starten
docker compose up -d --build

# 4. GUI oeffnen
open http://localhost:8088
```

## Erster Start

Beim ersten Start wird automatisch der Setup-Wizard angezeigt (`/setup`).
Er fuehrt durch:

1. **Paperless-Verbindung** — URL + Token pruefen
2. **Ollama-Verbindung** — URL pruefen, Modelle verifizieren
3. **Inbox-Tag** — Tag-ID fuer den Posteingang auswaehlen
4. **Erster Reindex** — Embedding-Index ueber alle bestehenden Dokumente aufbauen

Danach ist der Classifier betriebsbereit und beginnt automatisch zu pollen.

## Lokale Entwicklung (ohne Docker)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -c constraints.txt -e ".[dev]"
cp .env.example .env
# → Werte eintragen
uvicorn app.main:app --reload --port 8088
```

## Naechste Schritte

- [Konfiguration](./configuration.md) — Alle Umgebungsvariablen im Detail
- [CLI Commands](./cli.md) — Manuelle Pipeline-Steuerung
- [Review-Workflow](./workflow.md) — So funktioniert die Klassifikation
