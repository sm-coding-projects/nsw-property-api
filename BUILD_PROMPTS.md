# Build plan (phased)

Placeholder — the phased build plan with per-phase acceptance criteria lives here.

- Phase 0: repo init & toolchain (this phase)
- Phase 1: `pipeline/psi/` parser package (both PSI formats, keys, normalization)
- Phase 2: `backfill.py` — full-history local build → `data/nsw_property.db`
- Phase 3: Turso database creation & import
- Phase 4: `weekly_sync.py` + GitHub Actions weekly workflow + R2 archive
- Phase 5: Cloudflare Worker API (`api/`)
- Phase 6: `reconcile.py` + quarterly workflow, monitoring hookup
