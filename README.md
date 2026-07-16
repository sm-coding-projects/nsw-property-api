# NSW Property Sales API

Private JSON API serving NSW Valuer General property sales data (1990–present),
built on free tiers only: a one-time local backfill into SQLite, uploaded to Turso;
a weekly GitHub Actions sync that ingests new PSI files and archives raw ZIPs to
Cloudflare R2; and a Cloudflare Worker (Hono) exposing sales, locality summaries,
and metadata behind a single bearer token.

- Architecture, schema, data-source details, and working rules: [CLAUDE.md](CLAUDE.md)
- Phased build plan: [BUILD_PROMPTS.md](BUILD_PROMPTS.md)

## API usage

Base URL: `https://nsw-property-api.pblaise0990.workers.dev`. Every request
needs `Authorization: Bearer $API_TOKEN`. Errors come back as
`{"error": {"code": "...", "message": "..."}}`.

```sh
# Sales require postcode, locality, or a from+to date range (else 400).
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BASE/v1/sales?postcode=2010&limit=50"

# Optional filters: from, to (contract date), min_price, max_price.
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BASE/v1/sales?locality=parramatta&from=2025-01-01&to=2025-06-30&min_price=500000"

# Responses include next_cursor for keyset pagination (contract_date DESC):
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BASE/v1/sales?postcode=2010&cursor=eyJkIjoi..."

# One sale by key:
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BASE/v1/sales/C:001:2961978:1"

# Monthly price summary for a locality (reads precomputed aggregates only):
curl -H "Authorization: Bearer $API_TOKEN" \
  "$BASE/v1/localities/SURRY%20HILLS/summary?from=2024-01&to=2024-12"

# Row count and last sync (doubles as a health check):
curl -H "Authorization: Bearer $API_TOKEN" "$BASE/v1/meta"
```

GET responses are cached at the edge for 1 hour (the data changes weekly).
