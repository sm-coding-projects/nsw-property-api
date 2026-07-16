/**
 * NSW Property Sales API — private JSON API over the Turso database.
 *
 * Auth: `Authorization: Bearer <API_TOKEN>` on every route (timing-safe).
 * Errors: {"error": {"code": "...", "message": "..."}}
 *
 * EXPLAIN QUERY PLAN results (run against the production Turso DB,
 * 2026-07-16 — every sales query is an index SEARCH, never a SCAN):
 *
 *   /v1/sales?postcode=…  (+cursor)
 *     SEARCH sales USING INDEX idx_sales_postcode_date (postcode=? AND contract_date<?)
 *     USE TEMP B-TREE FOR RIGHT PART OF ORDER BY   -- sorts only same-date runs
 *   /v1/sales?locality=…  (+cursor)
 *     SEARCH sales USING INDEX idx_sales_locality_date (locality=? AND contract_date<?)
 *     USE TEMP B-TREE FOR RIGHT PART OF ORDER BY
 *   /v1/sales?from=…&to=…
 *     SEARCH sales USING INDEX idx_sales_contract_date (contract_date>? AND contract_date<?)
 *     USE TEMP B-TREE FOR RIGHT PART OF ORDER BY
 *   /v1/sales/:sale_key
 *     SEARCH sales USING COVERING INDEX sqlite_autoindex_sales_1 (sale_key=?)
 *   /v1/localities/:name/summary
 *     SEARCH locality_monthly USING INDEX sqlite_autoindex_locality_monthly_1 (locality=?)
 *     USE TEMP B-TREE FOR ORDER BY                 -- bounded: one locality's rows
 *   /v1/meta
 *     SCAN meta                                    -- 4-row table, not `sales`
 *
 * The cursor predicate is written as
 *   contract_date <= :d AND (contract_date < :d OR sale_key > :k)
 * (equivalent to the canonical OR form) so the index can range-bound
 * contract_date instead of re-reading newer rows on every page.
 */

import { Hono } from "hono";
import { createClient, type Client, type InValue } from "@libsql/client/web";

type Bindings = {
	TURSO_DATABASE_URL: string;
	TURSO_READ_TOKEN: string;
	API_TOKEN: string;
};

const app = new Hono<{ Bindings: Bindings }>();

// ---------- helpers ----------

const apiError = (status: 400 | 401 | 404 | 500, code: string, message: string) =>
	Response.json({ error: { code, message } }, { status });

async function timingSafeEqualStrings(a: string, b: string): Promise<boolean> {
	// Hash both sides to fixed-length digests, then constant-time compare.
	const enc = new TextEncoder();
	const [da, db] = await Promise.all([
		crypto.subtle.digest("SHA-256", enc.encode(a)),
		crypto.subtle.digest("SHA-256", enc.encode(b)),
	]);
	const ua = new Uint8Array(da);
	const ub = new Uint8Array(db);
	let diff = 0;
	for (let i = 0; i < ua.length; i++) diff |= ua[i] ^ ub[i];
	return diff === 0;
}

function db(env: Bindings): Client {
	// hrana-over-HTTP is the reliable transport from Workers.
	return createClient({
		url: env.TURSO_DATABASE_URL.replace(/^libsql:/, "https:"),
		authToken: env.TURSO_READ_TOKEN,
	});
}

const b64urlEncode = (s: string) =>
	btoa(s).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
const b64urlDecode = (s: string) =>
	atob(s.replaceAll("-", "+").replaceAll("_", "/"));

type Cursor = { d: string; k: string };

function encodeCursor(c: Cursor): string {
	return b64urlEncode(JSON.stringify(c));
}

function decodeCursor(raw: string): Cursor | null {
	try {
		const c = JSON.parse(b64urlDecode(raw));
		if (typeof c?.d === "string" && typeof c?.k === "string") return { d: c.d, k: c.k };
	} catch {
		/* fall through */
	}
	return null;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const MONTH_RE = /^\d{4}-\d{2}$/;
const POSTCODE_RE = /^\d{4}$/;

const SALE_COLUMNS =
	"sale_key, district_code, property_id, sale_counter, property_name, unit, " +
	"house_number, street, locality, postcode, area_sqm, contract_date, " +
	"settlement_date, price, zoning, nature_of_property, primary_purpose, " +
	"strata_lot, dealing_number, legal_description, source";

// ---------- middleware ----------

// Bearer auth on everything.
app.use("*", async (c, next) => {
	const header = c.req.header("Authorization") ?? "";
	const token = header.startsWith("Bearer ") ? header.slice(7) : "";
	if (!token || !(await timingSafeEqualStrings(token, c.env.API_TOKEN))) {
		return apiError(401, "unauthorized", "Missing or invalid bearer token.");
	}
	await next();
});

// Cache API on GETs, TTL 1h (data changes weekly). Runs after auth so
// unauthenticated requests never see cached bodies.
app.use("*", async (c, next) => {
	if (c.req.method !== "GET") return next();
	const cache = caches.default;
	const key = new Request(c.req.url);
	const hit = await cache.match(key);
	if (hit) return new Response(hit.body, hit);
	await next();
	if (c.res.status === 200) {
		const res = c.res.clone();
		const cached = new Response(res.body, res);
		cached.headers.set("Cache-Control", "public, max-age=3600");
		c.executionCtx.waitUntil(cache.put(key, cached));
	}
});

app.onError((err, c) => {
	console.error("unhandled error", err);
	return apiError(500, "internal", "Internal error.");
});

app.notFound(() => apiError(404, "not_found", "No such route."));

// ---------- routes ----------

app.get("/v1/sales", async (c) => {
	const q = c.req.query();

	const conds: string[] = [];
	const params: InValue[] = [];

	if (q.postcode !== undefined) {
		if (!POSTCODE_RE.test(q.postcode))
			return apiError(400, "bad_request", "postcode must be 4 digits.");
		conds.push("postcode = ?");
		params.push(q.postcode);
	}
	if (q.locality !== undefined) {
		conds.push("locality = ?");
		params.push(q.locality.toUpperCase());
	}
	for (const p of ["from", "to"] as const) {
		if (q[p] !== undefined && !DATE_RE.test(q[p]))
			return apiError(400, "bad_request", `${p} must be YYYY-MM-DD.`);
	}
	if (q.from) {
		conds.push("contract_date >= ?");
		params.push(q.from);
	}
	if (q.to) {
		conds.push("contract_date <= ?");
		params.push(q.to);
	}
	for (const p of ["min_price", "max_price"] as const) {
		if (q[p] !== undefined && !/^\d+$/.test(q[p]))
			return apiError(400, "bad_request", `${p} must be a non-negative integer.`);
	}
	if (q.min_price) {
		conds.push("price >= ?");
		params.push(Number(q.min_price));
	}
	if (q.max_price) {
		conds.push("price <= ?");
		params.push(Number(q.max_price));
	}

	// Quota guard: an unfiltered walk of `sales` is never allowed.
	if (!(q.postcode || q.locality || (q.from && q.to))) {
		return apiError(
			400,
			"missing_filter",
			"Provide postcode, locality, or both from and to.",
		);
	}

	let limit = 50;
	if (q.limit !== undefined) {
		if (!/^\d+$/.test(q.limit) || Number(q.limit) < 1)
			return apiError(400, "bad_request", "limit must be a positive integer.");
		limit = Math.min(Number(q.limit), 200);
	}

	if (q.cursor !== undefined) {
		const cur = decodeCursor(q.cursor);
		if (!cur) return apiError(400, "bad_cursor", "cursor is not valid.");
		// Index-friendly keyset predicate — see file header comment.
		conds.push("contract_date <= ?", "(contract_date < ? OR sale_key > ?)");
		params.push(cur.d, cur.d, cur.k);
	}

	const sql =
		`SELECT ${SALE_COLUMNS} FROM sales WHERE ${conds.join(" AND ")} ` +
		`ORDER BY contract_date DESC, sale_key ASC LIMIT ?`;
	params.push(limit + 1);

	const rs = await db(c.env).execute({ sql, args: params });
	const rows = rs.rows.slice(0, limit);
	const hasMore = rs.rows.length > limit;
	const last = rows[rows.length - 1] as { contract_date?: string; sale_key?: string } | undefined;

	return c.json({
		data: rows,
		next_cursor:
			hasMore && last?.contract_date && last?.sale_key
				? encodeCursor({ d: last.contract_date, k: last.sale_key })
				: null,
	});
});

app.get("/v1/sales/:sale_key", async (c) => {
	const rs = await db(c.env).execute({
		sql: `SELECT ${SALE_COLUMNS} FROM sales WHERE sale_key = ?`,
		args: [c.req.param("sale_key")],
	});
	if (rs.rows.length === 0) return apiError(404, "not_found", "No such sale.");
	return c.json({ data: rs.rows[0] });
});

app.get("/v1/localities/:name/summary", async (c) => {
	const name = c.req.param("name").toUpperCase();
	const q = c.req.query();
	for (const p of ["from", "to"] as const) {
		if (q[p] !== undefined && !MONTH_RE.test(q[p]))
			return apiError(400, "bad_request", `${p} must be YYYY-MM.`);
	}
	const from = q.from ?? "1990-01";
	const to = q.to ?? "9999-12";

	const rs = await db(c.env).execute({
		sql:
			"SELECT month, postcode, n_sales, median_price, mean_price, min_price, max_price " +
			"FROM locality_monthly WHERE locality = ? AND month >= ? AND month <= ? " +
			"ORDER BY month ASC, postcode ASC",
		args: [name, from, to],
	});
	if (rs.rows.length === 0)
		return apiError(404, "not_found", "No data for that locality and range.");

	let totalSales = 0;
	for (const r of rs.rows) totalSales += Number(r.n_sales);
	return c.json({
		locality: name,
		total_priced_sales: totalSales,
		months: rs.rows,
	});
});

app.get("/v1/meta", async (c) => {
	const rs = await db(c.env).execute("SELECT key, value FROM meta");
	const meta: Record<string, string | number> = {};
	for (const r of rs.rows) {
		const k = String(r.key);
		meta[k] = k === "row_count" ? Number(r.value) : String(r.value);
	}
	return c.json({
		row_count: meta.row_count ?? null,
		last_weekly_file: meta.last_weekly_file ?? null,
		last_sync_at: meta.last_sync_at ?? null,
	});
});

export default app;
