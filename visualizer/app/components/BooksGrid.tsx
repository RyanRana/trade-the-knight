"use client";

import { useBooks } from "@/lib/hooks";
import { OrderBookCard } from "./OrderBookCard";

export function BooksGrid({ ownerId }: { ownerId?: string }) {
  const { data, error, isLoading } = useBooks();

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Order Books</div>
          <div className="text-xs text-muted mt-0.5">
            {data.length ? `${data.length} markets` : "—"}
          </div>
        </div>
        <span className="pill">1 / 30s</span>
      </div>
      <div className="p-3">
        {error && <div className="text-down text-sm">{String(error.message ?? error)}</div>}
        {isLoading && !data.length && <div className="text-muted text-sm">Loading…</div>}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {data.map((b) => (
            <OrderBookCard key={b.symbol} book={b} highlightOwner={ownerId} />
          ))}
        </div>
      </div>
    </div>
  );
}
