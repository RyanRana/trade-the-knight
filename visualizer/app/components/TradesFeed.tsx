"use client";

import { useTrades } from "@/lib/hooks";
import { fmtNum, relTime } from "@/lib/format";

export function TradesFeed() {
  const { data, error, isLoading } = useTrades();
  const rows = data.slice().sort((a, b) => b.ts - a.ts).slice(0, 60);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Recent Trades</div>
          <div className="text-xs text-muted mt-0.5">public tape</div>
        </div>
        <span className="pill">1 / 60s</span>
      </div>
      <div className="max-h-[420px] overflow-auto">
        {error && <div className="p-4 text-down text-sm">{String(error.message ?? error)}</div>}
        {isLoading && !rows.length && <div className="p-4 text-muted text-sm">Loading…</div>}
        <table className="w-full text-xs">
          <thead className="text-muted uppercase tracking-wider">
            <tr>
              <th className="text-left px-3 py-2">Symbol</th>
              <th className="text-right px-3 py-2">Price</th>
              <th className="text-right px-3 py-2">Qty</th>
              <th className="text-right px-3 py-2">When</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => (
              <tr key={i} className="border-t border-border">
                <td className="px-3 py-1.5">{t.symbol}</td>
                <td className="px-3 py-1.5 num text-right">{fmtNum(t.price, 4)}</td>
                <td className="px-3 py-1.5 num text-right">{fmtNum(t.quantity, 2)}</td>
                <td className="px-3 py-1.5 num text-right text-muted">{relTime(t.ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
