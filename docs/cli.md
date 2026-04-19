# CLI Commands

Der Classifier stellt CLI-Befehle bereit, die im laufenden Container oder lokal
ausgefuehrt werden koennen. Sie sind nuetzlich fuer Wartung, Debugging und
manuelles Ausloesen der Pipeline-Phasen.

## Aufruf

```bash
# Installiert (Docker / pip install -e .)
archibot <command> [flags]

# Alternativ als Python-Modul
python -m app.cli <command> [flags]

# Im Docker-Container
docker exec -it archibot archibot <command> [flags]
```

## Befehle

### `reindex` — Voller Reindex

Loescht alle Embeddings und baut den gesamten Index neu auf.
Fuehrt optional OCR-Korrektur durch (wenn `OCR_MODE != off`).

```bash
archibot reindex
```

**Was passiert:**
1. Alle Eintraege in `doc_embeddings` und `doc_embedding_meta` werden geloescht
2. Phase 0: OCR-Korrektur fuer alle Dokumente (wenn aktiviert), Ergebnisse in `doc_ocr_cache`
3. Phase 1: Embedding fuer jedes Dokument berechnen und in die Vektor-DB schreiben

**Wann nutzen:** Nach Wechsel des Embedding-Modells, bei beschaedigter Vektor-DB,
oder beim ersten Setup.

---

### `reindex-ocr` — Nur OCR-Korrektur

Fuehrt OCR-Korrektur ueber alle Paperless-Dokumente aus, ohne Embeddings
neu zu berechnen. Respektiert die `OCR_MODE`-Einstellung.

```bash
# Nur neue Dokumente (Cache wird respektiert)
archibot reindex-ocr

# Alle Dokumente neu korrigieren (Cache ignorieren)
archibot reindex-ocr --force
```

**Flags:**
| Flag | Beschreibung |
|------|-------------|
| `--force` | OCR-Cache ignorieren und alle Dokumente neu korrigieren. Ohne dieses Flag werden bereits gecachte Korrekturen uebersprungen. |

**Was passiert:**
- Alle Dokumente aus Paperless werden geholt
- Fuer jedes Dokument wird `maybe_correct_ocr()` ausgefuehrt
- Ergebnisse landen in `doc_ocr_cache` (nie in Paperless)
- Bereits gecachte Korrekturen werden uebersprungen (ausser mit `--force`)

**Wann nutzen:** Nach Wechsel des OCR-Modells oder der OCR-Stufe.
Mit `--force` wenn vorhandene Korrekturen unbrauchbar sind und neu erzeugt werden sollen.

---

### `reindex-embed` — Nur Embeddings neu berechnen

Loescht alle Embeddings und berechnet sie neu. Nutzt gecachte OCR-Texte
aus `doc_ocr_cache` (falls vorhanden), fuehrt aber keine neue OCR-Korrektur durch.

```bash
archibot reindex-embed
```

**Was passiert:**
1. `doc_embeddings` und `doc_embedding_meta` werden geleert
2. Fuer jedes Dokument: OCR-Cache pruefen, dann Embedding berechnen

**Wann nutzen:** Nach Wechsel des Embedding-Modells, wenn OCR-Cache
bereits aktuell ist.

---

### `poll` — Inbox verarbeiten

Fuehrt einen einzelnen Poll-Durchlauf aus — identisch zum automatischen
Scheduler-Job, aber manuell ausgeloest.

```bash
archibot poll

# Inbox-Dokumente erneut verarbeiten (Idempotency-Skip ignorieren)
archibot poll --force
```

**Flags:**
| Flag | Beschreibung |
|------|-------------|
| `--force` | Ignoriert den Idempotency-Skip (`processed_documents`) und verarbeitet Inbox-Dokumente erneut, auch wenn sich `modified` nicht geaendert hat. |

**Was passiert:**
1. Dokumente mit Inbox-Tag aus Paperless holen
2. Phase 1: OCR-Korrektur (wenn `OCR_MODE != off`)
3. Phase 2: Embedding berechnen + Kontext-Dokumente finden
4. Phase 3: Klassifikation via LLM, Vorschlaege speichern

**Wann nutzen:** Zum Testen der Pipeline oder wenn man nicht auf den
naechsten automatischen Poll warten will.

---

### `process-doc` — Einzelnes Dokument verarbeiten

Fuehrt die komplette Pipeline fuer genau ein Dokument aus (OCR-Korrektur,
Embedding, Klassifikation, Vorschlag speichern / Auto-Commit nach Konfiguration).

```bash
# Ein Dokument verarbeiten
archibot process-doc 224

# Dokument erneut verarbeiten (Idempotency-Skip ignorieren)
archibot process-doc 224 --force
```

**Flags:**
| Flag | Beschreibung |
|------|-------------|
| `--force` | Loescht den bestehenden Eintrag in `processed_documents` fuer diese Dokument-ID und erzwingt dadurch eine Neuverarbeitung. |

**Wann nutzen:** Ideal fuer Debugging einzelner Faelle (z. B. fehlerhafte
Klassifikation oder Ollama-Probleme), ohne die gesamte Inbox zu starten.

---

### `reset` — Container zuruecksetzen

Loescht die gesamte Datenbank (Vorschlaege, Embeddings, OCR-Cache, Fehler,
Audit-Log, Tag-Whitelist/Blacklist) und erstellt eine leere DB neu.

```bash
# Nur Datenbank zuruecksetzen (Config behalten)
archibot reset --yes

# Datenbank + Config-Overrides zuruecksetzen (Werkseinstellungen)
archibot reset --yes --include-config
```

**Flags:**
| Flag | Beschreibung |
|------|-------------|
| `--yes` | **Pflicht.** Bestaetigt den Reset (keine interaktive Abfrage). |
| `--include-config` | Loescht zusaetzlich `config.env` und alle Backups. Verbindungseinstellungen (Paperless-URL, Token, Ollama-URL) gehen dabei verloren. |

**Was passiert:**
1. `classifier.db`, `-wal` und `-shm` Dateien werden geloescht
2. Optional: `config.env` und `config.bak.*` Backups werden geloescht
3. Eine leere Datenbank mit dem vollstaendigen Schema wird erstellt

**Was NICHT geloescht wird:**
- `.env` (Docker-Compose Umgebungsvariablen)
- Prompt-Dateien in `prompts/`
- Daten in Paperless-NGX selbst (Dokumente, Tags, etc.)

**Wann nutzen:** Bei einem Neustart von Grund auf, nach schwerwiegenden
Datenbank-Problemen, oder beim Wechsel der gesamten Klassifikationsstrategie.

## Hilfe

```bash
archibot --help
```

Zeigt alle verfuegbaren Befehle mit Kurzbeschreibung an.
