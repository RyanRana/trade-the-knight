"use client";

import { useEffect, useState } from "react";
import { useBooks, useLeaderboard, useTrades } from "@/lib/hooks";

export function StatusBar() {
  const { data: lb, isLoading: lbLoading, error: lbErr } = useLeaderboard();
  const { data: books, isLoading: booksLoading, error: booksErr } = useBooks();
  const { data: trades, isLoading: tradesLoading, error: tradesErr } = useTrades();
  const [now, setNow] = useState<string>("");

  useEffect(() => {
    const tick = () => setNow(new Date().toLocaleTimeString());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const status = (loading: boolean, err: unknown) =>
    err ? "err" : loading ? "…" : "ok";
  const colorOf = (loading: boolean, err: unknown) =>
    err ? "bg-down" : loading ? "bg-muted" : "bg-up";

  return (
    <div className="flex items-center gap-3 text-xs text-muted">
      <span className="num">{now}</span>
      <span>
        <span className={`dot ${colorOf(lbLoading, lbErr)}`} />
        leaderboard {status(lbLoading, lbErr)}
        {lb.length > 0 && <span className="ml-1 text-text">({lb.length})</span>}
      </span>
      <span>
        <span className={`dot ${colorOf(booksLoading, booksErr)}`} />
        books {status(booksLoading, booksErr)}
        {books.length > 0 && <span className="ml-1 text-text">({books.length})</span>}
      </span>
      <span>
        <span className={`dot ${colorOf(tradesLoading, tradesErr)}`} />
        trades {status(tradesLoading, tradesErr)}
        {trades.length > 0 && <span className="ml-1 text-text">({trades.length})</span>}
      </span>
    </div>
  );
}
