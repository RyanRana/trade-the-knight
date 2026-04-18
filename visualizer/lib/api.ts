export async function fetchJson<T>(path: string): Promise<T> {
  const r = await fetch(`/api/proxy/${path}`, { cache: "no-store" });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${text}`);
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
