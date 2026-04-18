"use client";

import { useLeaderboard } from "@/lib/hooks";
import { fmtUsd } from "@/lib/format";

export function Leaderboard({ teamName }: { teamName?: string }) {
  const { data, error, isLoading } = useLeaderboard();
  const top = data[0]?.equity ?? 0;

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Leaderboard</div>
          <div className="text-xs text-muted mt-0.5">Team equity, live</div>
        </div>
        <span className="pill">1 / 30s</span>
      </div>
      <div className="max-h-[520px] overflow-auto">
        {error && <div className="p-4 text-down text-sm">{String(error.message ?? error)}</div>}
        {isLoading && !data.length && <div className="p-4 text-muted text-sm">Loading…</div>}
        {!isLoading && !data.length && !error && (
          <div className="p-4 text-muted text-sm">No leaderboard data.</div>
        )}
        <table className="w-full text-sm">
          <thead className="text-muted text-xs uppercase tracking-wider">
            <tr>
              <th className="text-left px-4 py-2 w-10">#</th>
              <th className="text-left px-4 py-2">Team</th>
              <th className="text-right px-4 py-2">Equity</th>
              <th className="text-right px-4 py-2 w-24">vs #1</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row) => {
              const isUs =
                !!teamName && row.team.toLowerCase() === teamName.toLowerCase();
              const pct = top ? row.equity / top : 0;
              return (
                <tr
                  key={`${row.rank}-${row.team}`}
                  className={`border-t border-border ${isUs ? "bg-accent/10" : ""}`}
                >
                  <td className="px-4 py-2 num text-muted">{row.rank}</td>
                  <td className="px-4 py-2">
                    {isUs && <span className="dot bg-accent" />}
                    <span className={isUs ? "text-accent font-semibold" : ""}>{row.team}</span>
                  </td>
                  <td className="px-4 py-2 num text-right">{fmtUsd(row.equity)}</td>
                  <td className="px-4 py-2 num text-right text-muted">
                    {(pct * 100).toFixed(0)}%
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
