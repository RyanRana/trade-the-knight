export class ApiError extends Error {
  status: number;
  retryAfterMs: number | null;
  constructor(status: number, message: string, retryAfterMs: number | null) {
    super(message);
    this.status = status;
    this.retryAfterMs = retryAfterMs;
  }
}

function parseRetryAfter(h: string | null): number | null {
  if (!h) return null;
  const s = Number(h);
  if (Number.isFinite(s)) return s * 1000;
  const d = Date.parse(h);
  return Number.isFinite(d) ? Math.max(0, d - Date.now()) : null;
}

export async function fetchJson<T>(path: string): Promise<T> {
  const r = await fetch(`/api/proxy/${path}`, { cache: "no-store" });
  if (!r.ok) {
    const text = await r.text();
    const retryAfterMs = parseRetryAfter(r.headers.get("retry-after"));
    throw new ApiError(r.status, `${r.status} ${r.statusText}: ${text}`, retryAfterMs);
  }
  return r.json();
}

export const endpoints = {
  leaderboard: () => fetchJson<unknown>("leaderboard"),
  book: () => fetchJson<unknown>("book"),
  trades: () => fetchJson<unknown>("trades"),
  tape: () => fetchJson<unknown>("tape"),
  timeseries: () => fetchJson<unknown>("timeseries"),
  timeseriesData: (name: string, limit = 200) =>
    fetchJson<unknown>(`timeseries/${encodeURIComponent(name)}/data?limit=${limit}`),
};
