"use client";

import { useTape } from "@/lib/hooks";
import { fmtNum, relTime } from "@/lib/format";

export function TapeFeed() {
  const { data, error, isLoading } = useTape();
  const rows = data.slice().sort((a, b) => b.ts - a.ts).slice(0, 80);

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Tape</div>
          <div className="text-xs text-muted mt-0.5">order audit events</div>
        </div>
        <span className="pill">1 / 60s</span>
      </div>
      <div className="max-h-[420px] overflow-auto font-mono text-[11px] leading-relaxed">
        {error && <div className="p-4 text-down">{String(error.message ?? error)}</div>}
        {isLoading && !rows.length && <div className="p-4 text-muted">Loading…</div>}
        <div className="px-3 py-2 space-y-0.5">
          {rows.map((t, i) => (
            <div
              key={i}
              className="grid grid-cols-[auto_auto_1fr_auto_auto] gap-2 items-baseline"
            >
              <span className="text-muted w-16">{relTime(t.ts)}</span>
              <span className={t.side === "buy" ? "text-up" : "text-down"}>
                {t.side.toUpperCase().padEnd(4)}
              </span>
              <span>{t.symbol}</span>
              <span className="num text-right">{fmtNum(t.price, 4)}</span>
              <span className="text-muted text-right">{t.reason}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
