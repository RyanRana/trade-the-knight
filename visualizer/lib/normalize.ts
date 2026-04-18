import type {
  Book,
  BookLevel,
  LeaderboardEntry,
  TapeEvent,
  TimeseriesMeta,
  TimeseriesPoint,
  Trade,
} from "./types";

function asArray<T = unknown>(x: unknown): T[] {
  if (Array.isArray(x)) return x as T[];
  if (x && typeof x === "object") return Object.values(x as Record<string, T>);
  return [];
}

function num(x: unknown, d = 0): number {
  const n = typeof x === "string" ? parseFloat(x) : (x as number);
  return Number.isFinite(n) ? (n as number) : d;
}

function str(x: unknown): string {
  return x == null ? "" : String(x);
}

export function normalizeLeaderboard(raw: unknown): LeaderboardEntry[] {
  const items = asArray<Record<string, unknown>>(
    (raw as { leaderboard?: unknown; entries?: unknown; data?: unknown } | null)?.leaderboard ??
      (raw as { entries?: unknown })?.entries ??
      (raw as { data?: unknown })?.data ??
      raw,
  );
  return items
    .map((it, i) => ({
      rank: num(it.rank ?? i + 1, i + 1),
      team: str(it.team ?? it.team_name ?? it.name),
      team_id: it.team_id ? str(it.team_id) : undefined,
      equity: num(it.equity ?? it.total_equity ?? it.value),
      rud: it.rud != null ? num(it.rud) : undefined,
      inventory_value: it.inventory_value != null ? num(it.inventory_value) : undefined,
      bailout_penalty: it.bailout_penalty != null ? num(it.bailout_penalty) : undefined,
    }))
    .filter((e) => e.team)
    .sort((a, b) => a.rank - b.rank);
}

function normalizeLevels(x: unknown): BookLevel[] {
  if (!x) return [];
  if (Array.isArray(x)) {
    return x
      .map((lvl) => {
        if (Array.isArray(lvl)) {
          return { price: num(lvl[0]), quantity: num(lvl[1]), owner: lvl[2] ? str(lvl[2]) : undefined };
        }
        const o = lvl as Record<string, unknown>;
        return {
          price: num(o.price ?? o.p),
          quantity: num(o.quantity ?? o.qty ?? o.q ?? o.size),
          owner: o.owner != null ? str(o.owner) : undefined,
        };
      })
      .filter((l) => l.price > 0);
  }
  if (typeof x === "object") {
    return Object.entries(x as Record<string, unknown>)
      .map(([price, qty]) => {
        if (qty && typeof qty === "object" && !Array.isArray(qty)) {
          const o = qty as Record<string, unknown>;
          return {
            price: num(price),
            quantity: num(o.quantity ?? o.qty ?? o.q ?? o.size ?? 0),
            owner: o.owner != null ? str(o.owner) : undefined,
          };
        }
        return { price: num(price), quantity: num(qty) };
      })
      .filter((l) => l.price > 0);
  }
  return [];
}

export function normalizeBooks(raw: unknown): Book[] {
  if (!raw) return [];
  const root =
    (raw as { book?: unknown; books?: unknown; data?: unknown }).book ??
    (raw as { books?: unknown }).books ??
    (raw as { data?: unknown }).data ??
    raw;

  const result: Book[] = [];
  const push = (symbol: string, o: Record<string, unknown>) => {
    const bids = normalizeLevels(o.bids).sort((a, b) => b.price - a.price);
    const asks = normalizeLevels(o.asks).sort((a, b) => a.price - b.price);
    result.push({
      symbol,
      bids,
      asks,
      last_price: o.last_price != null ? num(o.last_price) : undefined,
      tick: o.tick != null ? num(o.tick) : undefined,
    });
  };

  if (Array.isArray(root)) {
    for (const b of root) {
      const o = b as Record<string, unknown>;
      const sym = str(o.symbol ?? o.asset ?? o.name);
      if (sym) push(sym, o);
    }
  } else if (root && typeof root === "object") {
    for (const [sym, b] of Object.entries(root as Record<string, unknown>)) {
      if (b && typeof b === "object") push(sym, b as Record<string, unknown>);
    }
  }
  return result.sort((a, b) => a.symbol.localeCompare(b.symbol));
}

export function normalizeTrades(raw: unknown): Trade[] {
  const items = asArray<Record<string, unknown>>(
    (raw as { trades?: unknown; data?: unknown } | null)?.trades ??
      (raw as { data?: unknown } | null)?.data ??
      raw,
  );
  return items
    .map((it) => ({
      symbol: str(it.symbol ?? it.asset),
      price: num(it.price),
      quantity: num(it.quantity ?? it.qty ?? it.q),
      ts: num(it.ts ?? it.timestamp ?? it.time ?? 0),
      tick: it.tick != null ? num(it.tick) : undefined,
      side: (it.side as Trade["side"]) ?? undefined,
    }))
    .filter((t) => t.symbol);
}

export function normalizeTape(raw: unknown): TapeEvent[] {
  const items = asArray<Record<string, unknown>>(
    (raw as { tape?: unknown; events?: unknown; data?: unknown } | null)?.tape ??
      (raw as { events?: unknown })?.events ??
      (raw as { data?: unknown })?.data ??
      raw,
  );
  return items
    .map((it) => ({
      symbol: str(it.symbol ?? it.asset),
      side: (str(it.side).toLowerCase() as TapeEvent["side"]) || "buy",
      price: num(it.price),
      quantity: num(it.quantity ?? it.qty),
      reason: str(it.reason),
      ts: num(it.ts ?? it.timestamp ?? it.time ?? 0),
      tick: it.tick != null ? num(it.tick) : undefined,
    }))
    .filter((t) => t.symbol);
}

export function normalizeTimeseries(raw: unknown): TimeseriesMeta[] {
  const items = asArray<Record<string, unknown>>(
    (raw as { series?: unknown; timeseries?: unknown; data?: unknown } | null)?.series ??
      (raw as { timeseries?: unknown })?.timeseries ??
      (raw as { data?: unknown })?.data ??
      raw,
  );
  return items
    .map((it) => ({
      name: str(it.name ?? it.id),
      status: it.status != null ? str(it.status) : undefined,
      latest_value: it.latest_value != null ? num(it.latest_value) : undefined,
      latest_ts: it.latest_ts != null ? num(it.latest_ts) : undefined,
      unit: it.unit != null ? str(it.unit) : undefined,
    }))
    .filter((s) => s.name);
}

export function normalizeTimeseriesData(raw: unknown): TimeseriesPoint[] {
  const items = asArray<Record<string, unknown>>(
    (raw as { points?: unknown; data?: unknown } | null)?.points ??
      (raw as { data?: unknown })?.data ??
      raw,
  );
  return items
    .map((p) => ({ t: num(p.t ?? p.ts ?? p.timestamp), v: num(p.v ?? p.value) }))
    .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
    .sort((a, b) => a.t - b.t);
}
