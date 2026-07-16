# NSW Property Sales API

Private JSON API serving NSW Valuer General property sales data (1990–present), kept
current by a weekly cloud sync. Infrastructure cost target: **$0** (free tiers only).

Sole consumer: the repo owner. No public users, no auth system beyond a single bearer token.

## Architecture (settled decisions — do not relitigate)

| Component | Choice | Why |
|---|---|---|
| One-time backfill | Python on the owner's Mac → local SQLite → wholesale upload | Full-history parse is heavy; Turso accepts whole SQLite file uploads |
| Database | **Turso** (managed libSQL/SQLite), single database | Only free tier that fits full 1990–now history in one DB (5 GB storage); `turso db import` seeds from a local file |
| Weekly sync | **GitHub Actions** scheduled workflow (public repo) | Unlimited free minutes on public repos; job is ~3–5 min/week |
| API | **Cloudflare Worker** + Hono + `@libsql/client/web` | 100k req/day free, global edge, secrets support, `*.workers.dev` URL is free |
| Raw archive | **Cloudflare R2** | 10 GB free, zero egress; enables full rebuild if upstream or DB provider changes |
| Monitoring | GitHub Actions failure emails + healthchecks.io dead-man ping | Free; catches both "job failed" and "job never ran" |

Rejected: Cloudflare D1 (free plan caps each DB at 500 MB — full history doesn't fit;
100k rows written/day would throttle the backfill), Supabase (500 MB + pauses after 7
days idle), Neon (0.5 GB/project), self-managed VMs (ops burden).

**Hard rule: no AI/LLM calls anywhere in the runtime pipeline or API.** Claude Code
builds and operates this system; the system itself is deterministic code.

## Free-tier quotas (verified July 2026 — re-verify before major changes)

- Turso Free: 5 GB storage, 500M row *reads*/month, 10M row *writes*/month. Row reads
  = rows **scanned**, so unindexed queries and aggregates are expensive. Every new
  query must pass an `EXPLAIN QUERY PLAN` check (no `SCAN TABLE` on `sales`).
- Cloudflare Workers Free: 100k requests/day. R2 Free: 10 GB storage, free egress.
- GitHub Actions: unlimited minutes on public repos. **Scheduled workflows are
  auto-disabled after 60 days without repo activity** — the weekly job's `state.json`
  commit is the keepalive. If a "workflow disabled" email ever arrives, re-enable it
  in the Actions tab.
- Actions cron runs are delayed 10–30+ min routinely. Irrelevant at weekly cadence;
  never assume exact start times.

## Data source

Publisher: NSW Valuer General, Property Sales Information (PSI) bulk data.

- Weekly files: `https://www.valuergeneral.nsw.gov.au/__psi/weekly/YYYYMMDD.zip`
  (Monday-dated). Yearly archives: `https://www.valuergeneral.nsw.gov.au/__psi/yearly/YYYY.zip`.
- ZIPs contain `.DAT` files (sometimes inside nested ZIPs). Records are
  semicolon-delimited lines: `B;` = sale record, `C;` = property legal description
  (join to B on district code + property ID + sale counter).
- **Two formats.** Current (2001–now): numeric property ID in field 3; dates as
  `YYYYMMDD`. Archived (1990–2001): alphabetic source tag (e.g. `ARCHIVE`) in field 3;
  dates as `DD/MM/YYYY`; different field layout; no download datetime.
- Weekly files are keyed by **entry date, not contract date** — a file contains sales
  entered that week, including contracts from months earlier and corrections to
  previously published rows. Recent weeks' files can change after first publication.
  Reference implementation (jameselks/nsw-property-sales-data-cleaner) deliberately
  skips the newest 14 days. Our approach instead: re-scan a **trailing 10-week
  window** every run and upsert.
- Some records have no price (non-market transfers) — keep them, price = NULL.
- Filter obviously bad dates: contract_date must be within 1990-01-01..today.
- The Valuer General's official format PDFs are vendored in the jameselks repo under
  `/Valuer General documentation/` — consult them when field semantics are unclear.
- **Licensing:** before any public exposure of this data or API, read the terms of
  use on the VG download page (attribution and reuse conditions). Private use by the
  owner is the current scope.

## Identity & dedup rules

Primary key `sale_key` (TEXT):

- Current format: `C:{district_code}:{property_id}:{sale_counter}`
- Archived format (no stable IDs): `A:` + sha1 over the normalized tuple
  `(district_code, address fields, contract_date, price)` — deterministic, so
  re-parses are idempotent.

Conflict resolution:

- Current-format rows: `INSERT ... ON CONFLICT(sale_key) DO UPDATE ... WHERE
  excluded.download_datetime > sales.download_datetime` (newest download wins).
- Archived rows: `INSERT OR IGNORE` (yearly archives are static).
- The same sale may appear in both a weekly file and a yearly archive — same
  `sale_key`, so the upsert dedupes naturally.

## Database schema (source of truth)

```sql
CREATE TABLE sales (
  sale_key          TEXT PRIMARY KEY,
  district_code     TEXT,
  property_id       TEXT,            -- NULL for archived rows
  sale_counter      TEXT,            -- NULL for archived rows
  download_datetime TEXT,            -- NULL for archived rows
  property_name     TEXT,
  unit              TEXT,
  house_number      TEXT,
  street            TEXT,
  locality          TEXT,            -- suburb, uppercased on ingest
  postcode          TEXT,
  area_sqm          REAL,            -- 'H' area_type × 10,000 at parse time
  contract_date     TEXT,            -- ISO YYYY-MM-DD
  settlement_date   TEXT,            -- ISO YYYY-MM-DD
  price             INTEGER,         -- whole dollars; NULL = non-market transfer
  zoning            TEXT,
  nature_of_property TEXT,
  primary_purpose   TEXT,
  strata_lot        TEXT,
  dealing_number    TEXT,
  legal_description TEXT,
  source            TEXT CHECK (source IN ('current','archived')),
  source_file       TEXT,
  ingested_at       TEXT
);
CREATE INDEX idx_sales_postcode_date ON sales(postcode, contract_date);
CREATE INDEX idx_sales_locality_date ON sales(locality, contract_date);
CREATE INDEX idx_sales_contract_date ON sales(contract_date);

-- Precomputed aggregates (SQLite has no median(); computed in Python).
-- Exists so summary endpoints never scan the sales table.
CREATE TABLE locality_monthly (
  locality     TEXT,
  postcode     TEXT,
  month        TEXT,                 -- YYYY-MM
  n_sales      INTEGER,              -- priced sales only
  median_price INTEGER,
  mean_price   INTEGER,
  min_price    INTEGER,
  max_price    INTEGER,
  PRIMARY KEY (locality, postcode, month)
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- keys: schema_version, row_count, last_weekly_file, last_sync_at
```

Before any Turso upload the local DB must be in WAL mode with a truncating
checkpoint: `PRAGMA journal_mode=WAL;` then `PRAGMA wal_checkpoint(TRUNCATE);`.

## Repo layout

```
.
├── CLAUDE.md
├── .mcp.json                  # project-scope MCP servers (no secrets)
├── BUILD_PROMPTS.md           # phased build plan
├── Makefile
├── pipeline/                  # Python 3.12+
│   ├── psi/                   # shared parser package (both formats, keys, normalize)
│   ├── backfill.py            # one-time: download all years, build data/nsw_property.db
│   ├── weekly_sync.py         # cloud: trailing-window fetch → upsert → R2 → state
│   ├── reconcile.py           # quarterly: yearly archive vs Turso drift check
│   └── tests/
├── api/                       # Cloudflare Worker (TypeScript, Hono)
│   ├── src/index.ts
│   └── wrangler config (as scaffolded by create-cloudflare)
├── .github/workflows/
│   ├── weekly-sync.yml        # cron: '30 19 * * 2'  (Tue 19:30 UTC ≈ Wed 05:30–06:30 Sydney)
│   └── reconcile.yml          # cron: '0 20 5 1,4,7,10 *' + workflow_dispatch
├── state/state.json           # {"<file>": {"sha256":…, "rows":…, "processed_at":…}}
└── data/                      # gitignored: raw zips + nsw_property.db
```

`pipeline/psi/` is imported by backfill, weekly sync, and reconcile — one parser,
three callers. Never fork the parsing logic.

## Weekly sync algorithm (`weekly_sync.py`)

1. Compute candidate Mondays for the trailing 10 weeks.
2. For each: download the ZIP; sha256 it; skip if hash matches `state/state.json`.
   A 404 for the most recent Monday(s) is normal (not yet published) — not an error.
3. Parse changed files via `psi`; upsert in batches (~500 rows/statement) to Turso.
4. Recompute `locality_monthly` only for (locality, postcode, month) pairs touched.
5. Update `meta` (row_count, last_weekly_file, last_sync_at).
6. Upload raw ZIPs to R2 at `raw/weekly/YYYYMMDD.zip` (S3-compatible API).
7. Write `state/state.json`; workflow commits it (this is also the cron keepalive).
8. Ping `$HEALTHCHECK_URL` on success; `$HEALTHCHECK_URL/fail` on failure.

## API contract (Worker)

Auth: `Authorization: Bearer <API_TOKEN>` on every route; timing-safe comparison;
401 otherwise. Errors: `{"error": {"code": "...", "message": "..."}}`.

- `GET /v1/sales` — filters: `postcode`, `locality`, `from`, `to` (contract_date),
  `min_price`, `max_price`. **Requires** `postcode` or `locality` or (`from` and
  `to`) — otherwise 400 (protects the Turso read quota). Keyset pagination ordered
  by `(contract_date DESC, sale_key)`: `limit` (default 50, max 200) + opaque
  `cursor`; response includes `next_cursor`.
- `GET /v1/sales/:sale_key` — single record.
- `GET /v1/localities/:name/summary?from=&to=` — reads `locality_monthly` only.
- `GET /v1/meta` — row_count, last_weekly_file, last_sync_at (doubles as health check).

Optional: Cache API on GETs, TTL 1h (data changes weekly).

## Secrets inventory (never commit any of these)

| Secret | Lives in |
|---|---|
| `TURSO_DATABASE_URL` | GH Actions secret + Worker var |
| `TURSO_WRITE_TOKEN` (full access) | GH Actions secret only |
| `TURSO_READ_TOKEN` (read-only) | Worker secret only |
| `API_TOKEN` (long random bearer) | Worker secret + owner's password manager |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | GH Actions secrets |
| `HEALTHCHECK_URL` | GH Actions secret |
| `GITHUB_PAT` (only for the optional GitHub MCP server) | Local shell env only |

`.gitignore` must cover: `data/`, `*.db`, `*.db-wal`, `*.db-shm`, `.env`, `.dev.vars`,
`node_modules/`, `__pycache__/`, `.venv/`, `.wrangler/`.

## Commands

```
make backfill      # pipeline/backfill.py → data/nsw_property.db + qa_report.md
make test          # pytest pipeline/tests
make sync          # run weekly_sync.py locally (needs env vars)
make deploy        # wrangler deploy from api/
turso db shell nsw-property "select count(*) from sales"
```

## Non-negotiable working rules for Claude Code

1. **Verify CLI syntax before use** — `turso --help`, `wrangler --help`, `gh --help`,
   and official docs. Flags in this file may have drifted; the CLIs are the truth.
2. Never embed an Anthropic/Claude API call in `pipeline/` or `api/`.
3. Every new SQL query: run `EXPLAIN QUERY PLAN`; reject full scans of `sales`.
4. Never print or commit secret values; reference them by env var name only.
5. Don't hand-edit `state/state.json` semantics — the weekly job owns it.
6. Batch DB writes; respect Turso's monthly write quota (index writes count too).
7. Prefer boring, dependency-light Python (requests, boto3, stdlib sqlite3).
8. When a phase's acceptance criteria (BUILD_PROMPTS.md) aren't met, fix before
   moving on — later phases assume earlier ones are solid.
