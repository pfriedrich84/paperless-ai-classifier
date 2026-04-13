---
description: "Pre-Commit-Checks ausfuehren (Lint, Format, Tests)"
---

Fuehre die drei Pre-Commit-Checks fuer dieses Projekt aus:

1. **Lint:** `ruff check app/ tests/`
2. **Format:** `ruff format --check app/ tests/`
3. **Tests:** `pytest tests/ -v`

Falls Lint oder Format fehlschlagen:
- Fuehre `ruff format app/ tests/` aus (Auto-Fix Formatting)
- Fuehre `ruff check --fix app/ tests/` aus (Auto-Fix Lint)
- Pruefe danach erneut mit `ruff check app/ tests/` und `ruff format --check app/ tests/`

Am Ende eine kurze Zusammenfassung ausgeben:
- Welche Checks bestanden / fehlgeschlagen sind
- Ob Auto-Fixes angewendet wurden
- Ob der Code jetzt commit-ready ist
