"use client";

import type { Book } from "@/lib/types";
import { fmtNum } from "@/lib/format";

export function OrderBookCard({ book, highlightOwner }: { book: Book; highlightOwner?: string }) {
  const bestBid = book.bids[0]?.price;
  const bestAsk = book.asks[0]?.price;
  const mid = bestBid && bestAsk ? (bestBid + bestAsk) / 2 : undefined;
  const spread = bestBid && bestAsk ? bestAsk - bestBid : undefined;
  const maxQty = Math.max(
    ...book.bids.slice(0, 8).map((l) => l.quantity),
    ...book.asks.slice(0, 8).map((l) => l.quantity),
    1,
  );
  const asks = book.asks.slice(0, 8).slice().reverse();
  const bids = book.bids.slice(0, 8);
  const ownsAny = (lvls: typeof bids) =>
    !!highlightOwner && lvls.some((l) => l.owner === highlightOwner);
  const hasOurOrder = ownsAny(asks) || ownsAny(bids);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="text-sm font-semibold">{book.symbol}</div>
          <div className="text-xs text-muted mt-0.5 num">
            {mid != null ? `mid ${fmtNum(mid, 4)}` : "—"}
            {spread != null && ` · spread ${fmtNum(spread, 4)}`}
          </div>
        </div>
        {hasOurOrder && <span className="pill text-accent border-accent/40">ours</span>}
      </div>
      <div className="p-3 text-xs num">
        {asks.map((l, i) => {
          const pct = (l.quantity / maxQty) * 100;
          const mine = l.owner === highlightOwner;
          return (
            <div key={`a-${i}`} className="relative grid grid-cols-[1fr_auto] gap-2 py-0.5">
              <div
                className="absolute inset-y-0 right-0 bg-down/10"
                style={{ width: `${pct}%` }}
              />
              <span className={`relative text-down ${mine ? "font-bold" : ""}`}>
                {fmtNum(l.price, 4)}
              </span>
              <span className="relative text-right text-muted">{fmtNum(l.quantity, 2)}</span>
            </div>
          );
        })}
        {mid != null && (
          <div className="my-1 text-center text-[11px] text-muted border-y border-border py-1">
            mid {fmtNum(mid, 4)}
          </div>
        )}
        {bids.map((l, i) => {
          const pct = (l.quantity / maxQty) * 100;
          const mine = l.owner === highlightOwner;
          return (
            <div key={`b-${i}`} className="relative grid grid-cols-[1fr_auto] gap-2 py-0.5">
              <div
                className="absolute inset-y-0 right-0 bg-up/10"
                style={{ width: `${pct}%` }}
              />
              <span className={`relative text-up ${mine ? "font-bold" : ""}`}>
                {fmtNum(l.price, 4)}
              </span>
              <span className="relative text-right text-muted">{fmtNum(l.quantity, 2)}</span>
            </div>
          );
        })}
        {!asks.length && !bids.length && (
          <div className="text-muted text-center py-4">empty book</div>
        )}
      </div>
    </div>
  );
}
