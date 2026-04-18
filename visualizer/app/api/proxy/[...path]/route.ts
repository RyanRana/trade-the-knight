import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BASE = process.env.EXCHANGE_BASE_URL?.replace(/\/$/, "") ?? "";
const KEY = process.env.EXCHANGE_API_KEY ?? "";

// Per-path rate limits documented by the exchange (ms).
const RATE_LIMIT_MS: Array<[RegExp, number]> = [
  [/^trades(\/|$)/, 60_000],
  [/^tape(\/|$)/, 60_000],
  [/^leaderboard(\/|$)/, 30_000],
  [/^book(\/|$)/, 30_000],
  [/^timeseries(\/|$)/, 30_000],
];

type CacheEntry = {
  body: string;
  contentType: string;
  status: number;
  fetchedAt: number;
  cooldownUntil: number;
};

// Module-scope cache survives across requests in a single Node process.
// Coalesces bursts from multiple tabs/components into one upstream call.
const cache = new Map<string, CacheEntry>();
const inflight = new Map<string, Promise<CacheEntry>>();

function rateLimitFor(path: string): number {
  for (const [re, ms] of RATE_LIMIT_MS) if (re.test(path)) return ms;
  return 30_000;
}

function parseRetryAfter(h: string | null): number | null {
  if (!h) return null;
  const s = Number(h);
  if (Number.isFinite(s)) return s * 1000;
  const d = Date.parse(h);
  return Number.isFinite(d) ? Math.max(0, d - Date.now()) : null;
}

async function fetchUpstream(key: string, target: string, cooldownMs: number): Promise<CacheEntry> {
  const r = await fetch(target, {
    headers: { "X-API-Key": KEY, Accept: "application/json" },
    cache: "no-store",
  });
  const body = await r.text();
  const contentType = r.headers.get("content-type") ?? "application/json";
  const now = Date.now();

  if (r.status === 429) {
    const retry = parseRetryAfter(r.headers.get("retry-after")) ?? cooldownMs;
    const prior = cache.get(key);
    if (prior) {
      // Extend the prior entry's cooldown so we stop hammering upstream.
      prior.cooldownUntil = now + retry;
      return prior;
    }
    const entry: CacheEntry = {
      body,
      contentType,
      status: 429,
      fetchedAt: now,
      cooldownUntil: now + retry,
    };
    cache.set(key, entry);
    return entry;
  }

  const entry: CacheEntry = {
    body,
    contentType,
    status: r.status,
    fetchedAt: now,
    cooldownUntil: now + cooldownMs,
  };
  cache.set(key, entry);
  return entry;
}

export async function GET(
  req: NextRequest,
  { params }: { params: { path: string[] } },
) {
  if (!BASE) {
    return NextResponse.json(
      { error: "EXCHANGE_BASE_URL is not set. Configure .env.local." },
      { status: 500 },
    );
  }
  if (!KEY) {
    return NextResponse.json(
      { error: "EXCHANGE_API_KEY is not set. Configure .env.local." },
      { status: 500 },
    );
  }

  const joined = params.path.join("/");
  const search = req.nextUrl.search ?? "";
  const key = `${joined}${search}`;
  const target = `${BASE}/api/exchange/public/${joined}${search}`;
  const cooldownMs = rateLimitFor(joined);
  const now = Date.now();

  const cached = cache.get(key);
  // Serve cache if we're still inside the upstream cooldown window.
  if (cached && now < cached.cooldownUntil && cached.status !== 429) {
    return respond(cached, target, true);
  }

  try {
    let pending = inflight.get(key);
    if (!pending) {
      pending = fetchUpstream(key, target, cooldownMs).finally(() => inflight.delete(key));
      inflight.set(key, pending);
    }
    const entry = await pending;

    // If upstream said 429 but we have usable older data, serve it instead.
    if (entry.status === 429 && cached && cached.status !== 429) {
      return respond(cached, target, true, Math.max(0, entry.cooldownUntil - Date.now()));
    }
    return respond(entry, target, false);
  } catch (err) {
    if (cached && cached.status !== 429) {
      return respond(cached, target, true);
    }
    return NextResponse.json(
      {
        error: "Upstream fetch failed",
        target,
        detail: err instanceof Error ? err.message : String(err),
      },
      { status: 502 },
    );
  }
}

function respond(entry: CacheEntry, target: string, fromCache: boolean, retryAfterMs?: number) {
  const headers: Record<string, string> = {
    "content-type": entry.contentType,
    "x-upstream-url": target,
    "x-cache": fromCache ? "HIT" : "MISS",
    "x-cache-age-ms": String(Math.max(0, Date.now() - entry.fetchedAt)),
  };
  const retry = retryAfterMs ?? Math.max(0, entry.cooldownUntil - Date.now());
  if (retry > 0) headers["retry-after"] = String(Math.ceil(retry / 1000));
  return new NextResponse(entry.body, { status: entry.status, headers });
}
