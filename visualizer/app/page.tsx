import { AssetsGrid } from "./components/AssetsGrid";
import { BooksGrid } from "./components/BooksGrid";
import { Leaderboard } from "./components/Leaderboard";
import { StatusBar } from "./components/StatusBar";
import { TapeFeed } from "./components/TapeFeed";
import { TimeseriesPanel } from "./components/TimeseriesPanel";
import { TradesFeed } from "./components/TradesFeed";

export default function Page() {
  const teamName = process.env.NEXT_PUBLIC_TEAM_NAME || undefined;
  const ownerId = process.env.NEXT_PUBLIC_TEAM_ID || undefined;

  return (
    <main className="p-4 md:p-6 space-y-4 max-w-[1800px] mx-auto">
      <header className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Knight Visualizer</h1>
          <p className="text-xs text-muted mt-0.5">
            Live exchange state · polled via public REST API
            {teamName && <> · team <span className="text-accent">{teamName}</span></>}
          </p>
        </div>
        <StatusBar />
      </header>

      <div className="grid grid-cols-1 2xl:grid-cols-[1fr_380px] gap-4">
        <div className="space-y-4">
          <AssetsGrid ownerId={ownerId} />
          <TimeseriesPanel />
          <BooksGrid ownerId={ownerId} />
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <TradesFeed />
            <TapeFeed />
          </div>
        </div>
        <div className="space-y-4">
          <Leaderboard teamName={teamName} />
        </div>
      </div>
    </main>
  );
}
