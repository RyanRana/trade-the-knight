# Knight Visualizer

Next.js dashboard that polls the exchange's public REST API and visualizes **work & compute across assets** — order books, recent trades, the tape, leaderboard, and public timeseries.

The public data surface is REST only (the websocket at `/ws/state` requires a bot credential, not the public API key). This app polls at the documented rate limits.

## Setup

```
cp .env.local.example .env.local
# edit .env.local and set:
#   EXCHANGE_BASE_URL  — the site host, e.g. https://your-comp.example.com
#   EXCHANGE_API_KEY   — your X-API-Key (already prefilled)
#   NEXT_PUBLIC_TEAM_NAME — optional, highlights your leaderboard row
#   NEXT_PUBLIC_TEAM_ID   — optional, marks books/assets where your team is active

npm install
npm run dev
```

Open http://localhost:3000.

## Endpoints polled

| Endpoint | Rate | Shown in |
| --- | --- | --- |
| `/api/exchange/public/leaderboard` | 1 / 30s | Leaderboard panel |
| `/api/exchange/public/book` | 1 / 30s | Books grid, Assets grid |
| `/api/exchange/public/trades` | 1 / 60s | Trades feed, Assets grid |
| `/api/exchange/public/tape` | 1 / 60s | Tape |
| `/api/exchange/public/timeseries` | 1 / 30s | Timeseries sidebar |
| `/api/exchange/public/timeseries/:name/data` | 1 / 30s | Timeseries chart |

## Architecture

- `app/api/proxy/[...path]/route.ts` — server-side proxy. All browser requests go to `/api/proxy/...`; the server adds `X-API-Key` and hits the exchange. The key never touches the browser.
- `lib/normalize.ts` — tolerant normalizers. The public payload shapes aren't strictly spec'd, so each normalizer accepts arrays or object maps and common field aliases.
- `lib/hooks.ts` — SWR hooks with per-endpoint refresh intervals matching the rate limits.
