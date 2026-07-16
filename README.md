# NSW Property Sales API

Private JSON API serving NSW Valuer General property sales data (1990–present),
built on free tiers only: a one-time local backfill into SQLite, uploaded to Turso;
a weekly GitHub Actions sync that ingests new PSI files and archives raw ZIPs to
Cloudflare R2; and a Cloudflare Worker (Hono) exposing sales, locality summaries,
and metadata behind a single bearer token.

- Architecture, schema, data-source details, and working rules: [CLAUDE.md](CLAUDE.md)
- Phased build plan: [BUILD_PROMPTS.md](BUILD_PROMPTS.md)
