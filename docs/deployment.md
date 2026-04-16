# Deployment

Anleitungen fuer verschiedene Deployment-Szenarien.

## Docker Compose (Standard)

Siehe [Installation](./installation.md) fuer die grundlegende Einrichtung.

Das Image wird automatisch ueber GitHub Container Registry bereitgestellt:

```
ghcr.io/pfriedrich84/paperless-ai-classifier:latest
```

### Verfuegbare Tags

| Tag | Beschreibung |
|---|---|
| `latest` | Aktueller Stand von `main` |
| `v0.1.0`, `v0.1` | Versionierte Releases |
| `sha-<hash>` | Spezifischer Commit |

## Deployment via Dockhand

Fuer Dockhand-basierte Setups (z.B. Homelab mit zentraler Stack-Verwaltung):

1. **Repo vorbereiten** — Privates GitHub-Repo `pfriedrich84/paperless-ai-classifier`
2. **Deploy Key** — SSH Deploy Key in GitHub hinterlegen (read-only)
3. **Dockhand konfigurieren:**
   - Settings → Git → Repo hinzufuegen
   - Stacks → Create from Git → Compose Path: `docker-compose.yml`
4. **Env-Datei bereitstellen** — Auf dem Docker-Host anlegen:
   ```bash
   mkdir -p /opt/stacks/paperless-ai-classifier
   # .env mit allen Variablen anlegen:
   nano /opt/stacks/paperless-ai-classifier/.env
   ```
   In Dockhand: External Env File → `/opt/stacks/paperless-ai-classifier/.env`
5. **Auto-Sync** — Aktivieren oder Webhook fuer automatische Updates einrichten

### Reverse Proxy

Der Classifier laeuft hinter Zoraxy (oder einem anderen Reverse Proxy).
Keine Ports direkt gegen das Internet freigeben.

Wichtig: Wenn Basic-Auth aktiviert ist (`GUI_USERNAME` / `GUI_PASSWORD`),
den Reverse Proxy so konfigurieren, dass er keine eigene Auth davor schaltet.

## Persistente Daten

Alle Daten liegen in `DATA_DIR` (Default: `/data`), das als Docker-Volume
gemountet werden sollte:

```yaml
volumes:
  - classifier-data:/data
```

### Inhalt von DATA_DIR

| Datei | Beschreibung |
|---|---|
| `classifier.db` | SQLite-Datenbank (Suggestions, Embeddings, OCR-Cache, Audit-Log) |
| `config.env` | Settings-UI-Overrides (hoechste Prioritaet) |
| `config.bak.*` | Automatische Backups von config.env |

### Backup

Fuer ein vollstaendiges Backup genuegt es, das `DATA_DIR`-Volume zu sichern:

```bash
# Docker-Volume-Backup
docker run --rm -v classifier-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/classifier-backup.tar.gz -C /data .
```

### Reset

Container-State zuruecksetzen: siehe [CLI-Dokumentation](./cli.md#reset).

```bash
# Nur DB zuruecksetzen
docker exec paperless-ai-classifier paperless-classify reset --yes

# Voller Factory-Reset (inkl. Config)
docker exec paperless-ai-classifier paperless-classify reset --yes --include-config
```

## Netzwerk-Anforderungen

| Verbindung | Richtung | Beschreibung |
|---|---|---|
| Classifier → Paperless | HTTP | API-Zugriff (Dokumente, Metadaten) |
| Classifier → Ollama | HTTP | LLM-Inference (Chat, Embedding) |
| Classifier → Telegram | HTTPS | Bot-API (optional, Long-Polling) |
| Browser → Classifier | HTTP | Web-GUI (Port 8088) |
| Paperless → Classifier | HTTP | Webhook (optional, Port 8088) |
