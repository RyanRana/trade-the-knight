"use client";

import { useEffect, useState } from "react";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTimeseriesData, useTimeseriesList } from "@/lib/hooks";
import { fmtNum, relTime } from "@/lib/format";

export function TimeseriesPanel() {
  const { data: list, error, isLoading } = useTimeseriesList();
  const [selected, setSelected] = useState<string | null>(null);
  const { data: points } = useTimeseriesData(selected, 300);

  useEffect(() => {
    if (!selected && list.length) {
      const ior = list.find((s) => s.name === "ior_rate");
      setSelected(ior?.name ?? list[0].name);
    }
  }, [list, selected]);

  const chartData = points.map((p) => ({
    t: p.t > 1e12 ? p.t : p.t * 1000,
    v: p.v,
  }));

  return (
    <div className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Timeseries</div>
          <div className="text-xs text-muted mt-0.5">
            {list.length ? `${list.length} public series` : "—"}
          </div>
        </div>
        <span className="pill">1 / 30s</span>
      </div>
      <div className="p-3 grid grid-cols-1 lg:grid-cols-[220px_1fr] gap-3">
        <div className="max-h-[360px] overflow-auto border border-border rounded">
          {error && <div className="p-2 text-down text-xs">{String(error.message ?? error)}</div>}
          {isLoading && !list.length && <div className="p-2 text-muted text-xs">Loading…</div>}
          {list.map((s) => (
            <button
              key={s.name}
              onClick={() => setSelected(s.name)}
              className={`w-full text-left px-3 py-2 text-xs border-b border-border hover:bg-panel2 ${
                selected === s.name ? "bg-panel2 text-accent" : ""
              }`}
            >
              <div className="flex justify-between items-baseline">
                <span className="font-semibold">{s.name}</span>
                <span className="num text-muted">{fmtNum(s.latest_value, 4)}</span>
              </div>
              <div className="text-[10px] text-muted">{relTime(s.latest_ts)}</div>
            </button>
          ))}
        </div>
        <div className="h-[360px] bg-panel2 rounded p-2">
          {!chartData.length && (
            <div className="h-full flex items-center justify-center text-muted text-sm">
              {selected ? "Loading series…" : "Select a series"}
            </div>
          )}
          {chartData.length > 0 && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 10, right: 16, bottom: 8, left: 8 }}>
                <XAxis
                  dataKey="t"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  tickFormatter={(v) => new Date(v).toLocaleTimeString()}
                  stroke="#7a809a"
                  fontSize={11}
                />
                <YAxis
                  stroke="#7a809a"
                  fontSize={11}
                  domain={["auto", "auto"]}
                  tickFormatter={(v) => fmtNum(v, 4)}
                />
                <Tooltip
                  contentStyle={{ background: "#161a2a", border: "1px solid #232842" }}
                  labelFormatter={(v) => new Date(Number(v)).toLocaleString()}
                  formatter={(v: number) => fmtNum(v, 6)}
                />
                <Line
                  type="monotone"
                  dataKey="v"
                  stroke="#7aa2ff"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                  name={selected ?? "value"}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}
