"use client";

import { useMemo } from "react";
import { useBooks, useTrades } from "@/lib/hooks";
import { fmtNum } from "@/lib/format";

type AssetRow = {
  symbol: string;
  last: number | undefined;
  bestBid: number | undefined;
  bestAsk: number | undefined;
  spread: number | undefined;
  depthBid: number;
  depthAsk: number;
  volume: number;
  trades: number;
  ownersActive: number;
  owners: Set<string>;
  oursActive: boolean;
};

export function AssetsGrid({ ownerId }: { ownerId?: string }) {
  const { data: books } = useBooks();
  const { data: trades } = useTrades();

  const rows = useMemo<AssetRow[]>(() => {
    const map = new Map<string, AssetRow>();
    for (const b of books) {
      const owners = new Set<string>();
      let oursActive = false;
      let depthBid = 0;
      let depthAsk = 0;
      for (const l of b.bids) {
        depthBid += l.quantity;
        if (l.owner) owners.add(l.owner);
        if (l.owner && ownerId && l.owner === ownerId) oursActive = true;
      }
      for (const l of b.asks) {
        depthAsk += l.quantity;
        if (l.owner) owners.add(l.owner);
        if (l.owner && ownerId && l.owner === ownerId) oursActive = true;
      }
      map.set(b.symbol, {
        symbol: b.symbol,
        last: b.last_price,
        bestBid: b.bids[0]?.price,
        bestAsk: b.asks[0]?.price,
        spread:
          b.bids[0]?.price != null && b.asks[0]?.price != null
            ? b.asks[0].price - b.bids[0].price
            : undefined,
        depthBid,
        depthAsk,
        volume: 0,
        trades: 0,
        ownersActive: owners.size,
        owners,
        oursActive,
      });
    }
    for (const t of trades) {
      const row =
        map.get(t.symbol) ??
        ({
          symbol: t.symbol,
          last: t.price,
          bestBid: undefined,
          bestAsk: undefined,
          spread: undefined,
          depthBid: 0,
          depthAsk: 0,
          volume: 0,
          trades: 0,
          ownersActive: 0,
          owners: new Set(),
          oursActive: false,
        } as AssetRow);
      row.volume += t.price * t.quantity;
      row.trades += 1;
      if (row.last == null) row.last = t.price;
      map.set(t.symbol, row);
    }
    return Array.from(map.values()).sort((a, b) => b.volume - a.volume || a.symbol.localeCompare(b.symbol));
  }, [books, trades, ownerId]);

  const totals = useMemo(() => {
    return rows.reduce(
      (acc, r) => {
        acc.volume += r.volume;
        acc.trades += r.trades;
        acc.depth += r.depthBid + r.depthAsk;
        acc.ours += r.oursActive ? 1 : 0;
        return acc;
      },
      { volume: 0, trades: 0, depth: 0, ours: 0 },
    );
  }, [rows]);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Work &amp; Compute · per asset</div>
          <div className="text-xs text-muted mt-0.5">
            {rows.length} assets · {totals.trades} trades · ${fmtNum(totals.volume, 0)} notional
            {ownerId && ` · ${totals.ours} ours`}
          </div>
        </div>
        <span className="pill">derived</span>
      </div>
      <div className="overflow-auto max-h-[520px]">
        <table className="w-full text-xs">
          <thead className="text-muted uppercase tracking-wider sticky top-0 bg-panel">
            <tr>
              <th className="text-left px-3 py-2">Symbol</th>
              <th className="text-right px-3 py-2">Last</th>
              <th className="text-right px-3 py-2">Bid</th>
              <th className="text-right px-3 py-2">Ask</th>
              <th className="text-right px-3 py-2">Spread</th>
              <th className="text-right px-3 py-2">Depth</th>
              <th className="text-right px-3 py-2">Trades</th>
              <th className="text-right px-3 py-2">Notional</th>
              <th className="text-right px-3 py-2">Makers</th>
              <th className="text-center px-3 py-2">Us</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr
                key={r.symbol}
                className={`border-t border-border ${r.oursActive ? "bg-accent/10" : ""}`}
              >
                <td className="px-3 py-1.5 font-semibold">{r.symbol}</td>
                <td className="px-3 py-1.5 num text-right">{fmtNum(r.last, 4)}</td>
                <td className="px-3 py-1.5 num text-right text-up">{fmtNum(r.bestBid, 4)}</td>
                <td className="px-3 py-1.5 num text-right text-down">{fmtNum(r.bestAsk, 4)}</td>
                <td className="px-3 py-1.5 num text-right">{fmtNum(r.spread, 4)}</td>
                <td className="px-3 py-1.5 num text-right">
                  <span className="text-up">{fmtNum(r.depthBid, 0)}</span>
                  <span className="text-muted mx-1">/</span>
                  <span className="text-down">{fmtNum(r.depthAsk, 0)}</span>
                </td>
                <td className="px-3 py-1.5 num text-right">{r.trades}</td>
                <td className="px-3 py-1.5 num text-right">{fmtNum(r.volume, 0)}</td>
                <td className="px-3 py-1.5 num text-right">{r.ownersActive}</td>
                <td className="px-3 py-1.5 text-center">
                  {r.oursActive ? <span className="text-accent">●</span> : <span className="text-muted">·</span>}
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td className="px-3 py-4 text-muted text-center" colSpan={10}>
                  No asset data yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
