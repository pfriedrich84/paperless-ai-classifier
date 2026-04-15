# Review-Workflow

So funktioniert die Klassifikation von der Dokumenterfassung bis zum Commit
in Paperless-NGX.

## Ablauf

```
1. Dokument hochladen          Paperless-NGX vergibt Tag "Posteingang"
         |
2. Worker erkennt Dokument      Naechster Poll oder Webhook-Trigger
         |
3. OCR-Korrektur (optional)     Nur wenn OCR_MODE != off
         |
4. Embedding berechnen          qwen3-embedding:0.6b, gespeichert in sqlite-vec
         |
5. Kontext-Suche                KNN: aehnlichste bereits klassifizierte Dokumente finden
         |
6. Klassifikation               LLM bekommt Zieldokument + Kontext, liefert JSON
         |
7. Vorschlag speichern          Status: "pending" in der suggestions-Tabelle
         |
   ┌─────┴──────┐
   |             |
8a. Auto-Commit               Wenn confidence >= AUTO_COMMIT_CONFIDENCE
8b. Manuelles Review          GUI (/review) oder Telegram
         |
9. PATCH nach Paperless         Metadaten-Update (Titel, Datum, Korrespondent, ...)
```

## Schritt fuer Schritt

### 1. Dokument wird erkannt

Der Worker pollt alle `POLL_INTERVAL_SECONDS` (Default: 300s) die Paperless-Inbox.
Alternativ kann ein [Webhook](./webhooks.md) sofortige Verarbeitung ausloesen.

Nur Dokumente mit dem Inbox-Tag (`PAPERLESS_INBOX_TAG_ID`) werden verarbeitet.
Bereits verarbeitete Dokumente (gleicher `updated_at`-Timestamp) werden uebersprungen.

### 2. Kontext-basierte Klassifikation

Der Classifier sucht per Embedding-Similarity die aehnlichsten bereits
klassifizierten Dokumente. Diese dienen als Few-Shot-Kontext:

- **Nur reviewte Dokumente** werden als Kontext genutzt — nie Inbox-Dokumente
- Kontext-Dokumente enthalten ihre **vollstaendige Klassifikation** (Korrespondent,
  Dokumenttyp, Speicherpfad, Tags, Datum)
- Das LLM nutzt diese als starke Hinweise fuer die eigene Entscheidung
- Anzahl der Kontext-Dokumente: `CONTEXT_MAX_DOCS` (Default: 5)

### 3. LLM-Vorschlag

Das LLM liefert strukturiertes JSON mit:
- **Titel** — bereinigter, aussagekraeftiger Titel
- **Datum** — erkanntes Dokumentdatum
- **Korrespondent** — Absender/Aussteller
- **Dokumenttyp** — Rechnung, Vertrag, Brief, etc.
- **Speicherpfad** — Ordner in Paperless
- **Tags** — passende Schlagworte
- **Confidence** — Vertrauenswert (0–100)
- **Reasoning** — Begruendung der Entscheidung

### 4. Review

#### In der GUI (`/review`)

- Alle offenen Vorschlaege in einer Queue
- Pro Vorschlag: Original vs. Vorschlag nebeneinander
- Felder einzeln editieren oder uebernehmen
- Annehmen oder Ablehnen mit einem Klick

#### Via Telegram (optional)

- Benachrichtigung mit Inline-Keyboard (Accept / Reject / Edit in GUI)
- Accept/Reject direkt im Chat, ohne die GUI zu oeffnen

#### Auto-Commit

Wenn `AUTO_COMMIT_CONFIDENCE > 0` und der LLM-Confidence-Score darueber liegt,
wird der Vorschlag automatisch committet — ohne manuelles Review.

### 5. Commit nach Paperless

Nach Freigabe werden die Metadaten via PATCH an Paperless geschrieben:
- Titel, Datum, Korrespondent, Dokumenttyp, Speicherpfad werden aktualisiert
- **Tags:** Nur Tags mit bekannter Paperless-ID werden geschrieben. Neue Tags
  landen in der Tag-Whitelist (`/tags`) und muessen erst freigegeben werden.
- **Inbox-Tag:** Bleibt standardmaessig erhalten (`KEEP_INBOX_TAG=true`).
  Mit `KEEP_INBOX_TAG=false` wird er beim Commit entfernt.
- **Processed-Tag:** Optional wird `PAPERLESS_PROCESSED_TAG_ID` hinzugefuegt.

## Tag-Management

### Whitelist

Neue Tags, die das LLM vorschlaegt und die noch nicht in Paperless existieren,
landen in der Tag-Whitelist mit Status `pending`. Auf der Seite `/tags` kannst du:

- **Freigeben** — Tag wird in Paperless angelegt und bei zukuenftigen Commits genutzt
- **Ablehnen** — Tag wandert in die Blacklist

### Blacklist

Abgelehnte Tags werden dauerhaft ignoriert. Das LLM kann sie weiterhin vorschlagen,
aber sie werden automatisch aus dem Vorschlag gefiltert. Tags koennen ueber `/tags`
wieder von der Blacklist entfernt werden.
