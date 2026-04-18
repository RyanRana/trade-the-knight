"use client";

import useSWR, { SWRConfiguration } from "swr";
import { ApiError, endpoints } from "./api";
import {
  normalizeBooks,
  normalizeLeaderboard,
  normalizeTape,
  normalizeTimeseries,
  normalizeTimeseriesData,
  normalizeTrades,
} from "./normalize";

// Documented rate limits: leaderboard/book/timeseries = 1/30s, trades/tape = 1/60s.
// Poll slightly slower than the limit to leave headroom for clock skew.
const R_30 = 33_000;
const R_60 = 63_000;

const baseConfig: SWRConfiguration = {
  revalidateOnFocus: false,
  revalidateOnReconnect: false,
  dedupingInterval: 5_000,
  keepPreviousData: true,
  shouldRetryOnError: true,
  errorRetryCount: 5,
  onErrorRetry: (err, _key, _cfg, revalidate, { retryCount }) => {
    // Respect upstream cooldown on 429, otherwise exponential backoff with jitter.
    const apiErr = err as ApiError;
    const base =
      apiErr?.status === 429 && apiErr.retryAfterMs
        ? apiErr.retryAfterMs
        : Math.min(30_000, 1_000 * 2 ** retryCount);
    const jitter = Math.random() * 500;
    setTimeout(() => revalidate({ retryCount }), base + jitter);
  },
};

export function useLeaderboard() {
  const { data, error, isLoading } = useSWR("leaderboard", endpoints.leaderboard, {
    ...baseConfig,
    refreshInterval: R_30,
  });
  return { data: data ? normalizeLeaderboard(data) : [], error, isLoading };
}

export function useBooks() {
  const { data, error, isLoading } = useSWR("book", endpoints.book, {
    ...baseConfig,
    refreshInterval: R_30,
  });
  return { data: data ? normalizeBooks(data) : [], error, isLoading };
}

export function useTrades() {
  const { data, error, isLoading } = useSWR("trades", endpoints.trades, {
    ...baseConfig,
    refreshInterval: R_60,
  });
  return { data: data ? normalizeTrades(data) : [], error, isLoading };
}

export function useTape() {
  const { data, error, isLoading } = useSWR("tape", endpoints.tape, {
    ...baseConfig,
    refreshInterval: R_60,
  });
  return { data: data ? normalizeTape(data) : [], error, isLoading };
}

export function useTimeseriesList() {
  const { data, error, isLoading } = useSWR("timeseries", endpoints.timeseries, {
    ...baseConfig,
    refreshInterval: R_30,
  });
  return { data: data ? normalizeTimeseries(data) : [], error, isLoading };
}

export function useTimeseriesData(name: string | null, limit = 200) {
  const { data, error, isLoading } = useSWR(
    name ? ["timeseries-data", name, limit] : null,
    () => endpoints.timeseriesData(name as string, limit),
    { ...baseConfig, refreshInterval: R_30 },
  );
  return { data: data ? normalizeTimeseriesData(data) : [], error, isLoading };
}
