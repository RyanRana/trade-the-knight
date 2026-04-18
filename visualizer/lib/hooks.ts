"use client";

import useSWR from "swr";
import { endpoints } from "./api";
import {
  normalizeBooks,
  normalizeLeaderboard,
  normalizeTape,
  normalizeTimeseries,
  normalizeTimeseriesData,
  normalizeTrades,
} from "./normalize";

// Documented rate limits: leaderboard/book/timeseries = 1/30s, trades/tape = 1/60s.
// We poll slightly slower than the limit to avoid 429s.
const R_30 = 31_000;
const R_60 = 61_000;

export function useLeaderboard() {
  const { data, error, isLoading } = useSWR("leaderboard", endpoints.leaderboard, {
    refreshInterval: R_30,
    revalidateOnFocus: false,
  });
  return { data: data ? normalizeLeaderboard(data) : [], error, isLoading };
}

export function useBooks() {
  const { data, error, isLoading } = useSWR("book", endpoints.book, {
    refreshInterval: R_30,
    revalidateOnFocus: false,
  });
  return { data: data ? normalizeBooks(data) : [], error, isLoading };
}

export function useTrades() {
  const { data, error, isLoading } = useSWR("trades", endpoints.trades, {
    refreshInterval: R_60,
    revalidateOnFocus: false,
  });
  return { data: data ? normalizeTrades(data) : [], error, isLoading };
}

export function useTape() {
  const { data, error, isLoading } = useSWR("tape", endpoints.tape, {
    refreshInterval: R_60,
    revalidateOnFocus: false,
  });
  return { data: data ? normalizeTape(data) : [], error, isLoading };
}

export function useTimeseriesList() {
  const { data, error, isLoading } = useSWR("timeseries", endpoints.timeseries, {
    refreshInterval: R_30,
    revalidateOnFocus: false,
  });
  return { data: data ? normalizeTimeseries(data) : [], error, isLoading };
}

export function useTimeseriesData(name: string | null, limit = 200) {
  const { data, error, isLoading } = useSWR(
    name ? ["timeseries-data", name, limit] : null,
    () => endpoints.timeseriesData(name as string, limit),
    { refreshInterval: R_30, revalidateOnFocus: false },
  );
  return { data: data ? normalizeTimeseriesData(data) : [], error, isLoading };
}
