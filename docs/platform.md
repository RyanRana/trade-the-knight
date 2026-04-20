Platform Reference

Runtime & Authentication
Competitor bots run inside the current `trading_bot` image built from `python:3.9-slim`. The platform mounts your uploaded script as /app/user_bot.py and injects BOT_ID and EXCHANGE_URL.

BOT_ID=<your bot id>
EXCHANGE_URL=http://127.0.0.1:3000
Competition-facing supported libraries: numpy, pandas, scipy, scikit-learn, statsmodels, ta-lib, and the bundled knight_trader files.
Per-bot limits enforced by the current runner: 256 KB upload size, 256 MB memory, and 0.25 CPU.
Bots are uploaded paused. Starting a bot launches the Docker container; if it exits immediately it is marked COMPLETED or CRASHED.
Trading auth uses the injected bot ID. The SDK sends it in the bot-facing auth path and the exchange treats that ID as the bot credential.
Competitors should treat the SDK as the supported network surface. The docs do not expose low-level transport libraries as part of the intended bot interface.

SDK Documentation
The bundled Python client is the intended competitor interface. It wraps the authenticated websocket and the order endpoints exposed by the Rust exchange.

ExchangeClient()
Reads BOT_ID and EXCHANGE_URL automatically, then starts a background websocket listener. BOT_ID is the bot credential used throughout the SDK.

client.stream_state() -> Generator[dict]
Yields the newest decoded state snapshot only. If your bot falls behind, older snapshots are dropped.

client.get_assets()
Returns the current asset table so your bot can discover tradable symbols, asset types, and lifecycle metadata.

client.get_book(symbol: Optional[str] = None) -> dict
Returns the latest cached book snapshot from SDK memory.

client.get_best_bid(symbol) / client.get_best_ask(symbol) / client.get_price(symbol)
Convenience helpers for top-of-book and midpoint reads from the cached state.

client.get_team_state() -> dict
Low-frequency coordination view for team treasury, bots, portfolio, recent trades, and open team orders.

client.buy(symbol, price, quantity) / client.sell(symbol, price, quantity)
Place limit orders. The SDK generates a client order ID, waits for the exchange acknowledgement delta, and returns the accepted order ID. Rejections return None.

client.cancel(order_id)
Cancels a resting order owned by the bot.

client.cancel_all()
Cancels all resting orders currently owned by the bot.

client.list_timeseries() / client.get_timeseries(name, limit=100)
Read public timeseries metadata and historical points from the agent runner.

client.place_auction_bid(symbol, yield_rate, quantity)
Submits a bond auction bid using the same bot identity as normal trading. The bid locks par capital at $1000 per requested unit.

Wire Format Note
The SDK connects to authenticated /ws/state, receives one protobuf snapshot on connect, and then applies protobuf deltas locally for books, timeseries, tape, and private order-status updates. If the stream gaps, it reconnects for a fresh snapshot before continuing.

Capital, Inventory & Valuation
`RUD` lives in the shared team treasury, but tradable inventory lives per bot. The dashboard allocates `RUD` from the team treasury into each bot's capital bucket. Buy orders consume only that bot's capital pool. Filled inventory belongs to the bot that acquired it.

Allocated bot capital is removed from the team RUD wallet and tracked on the bot row.
Buy orders lock price * quantity from the bot's uncommitted capital.
Tradable inventory is per bot. One bot cannot directly sell another bot's long inventory.
Naked shorting is allowed, but exposure is still enforced at the team level and uncovered shorts are charged at 1.5x market value.
Gross exposure is checked at the team level against the sum of all bot capital allocations, using last traded prices.
Leaderboard equity is team RUD plus per-bot inventory at last traded prices, plus total bot capital, minus bailout penalties.
If an asset has never traded, its marked value can still be zero even if there is a live order book.

Asset Behavior
RUD
Internal cash only. It is not listed as a tradable market.

Spot / Equities
Standard continuous order books. These are the simplest assets and behave exactly like ordinary spot markets.

Forex
Uses the same matching model as spot assets. The live UI formats them with FX-style decimals, but there is no separate conversion engine.

Prediction Markets
YES-share contracts bounded to prices between 0.01 and 0.99. YES resolution pays $1 per long share in RUD.

Options
Each option stores an underlying, strike, and call/put type. On expiry or admin resolution, it cash-settles to intrinsic value and open orders are cancelled.

Bonds
Issued through a yield auction at $1000 par per unit. The stop-out yield becomes the coupon rate. Coupon payments are credited in RUD. In the current implementation, later lifecycle steps such as maturity and WI conversion are admin-managed.
The platform also exposes public REST snapshots for the website and low-frequency tooling. They exist, but they are not the primary supported interface for competitor bots. For live bot logic, use ExchangeClient and stream_state().

Access Pattern
On the official website, these paths are served from the site origin. External callers should hit the site host, include the team API key in X-API-Key, and expect rate limits.

GET /api/exchange/public/book
1 / 30s
Full in-memory order book snapshot across symbols. Owner IDs are included.

GET /api/exchange/public/leaderboard
1 / 30s
Leaderboard entries with team name, total equity, and rank.

GET /api/exchange/public/trades
1 / 60s
Recent public trades. Buyer and seller IDs are scrubbed.

GET /api/exchange/public/tape
1 / 60s
Recent public order audit events with side, price, quantity, reason, timestamp, and tick.

GET /api/exchange/public/timeseries
1 / 30s
Lists public series metadata, status, latest value, and latest timestamp.

GET /api/exchange/public/timeseries/:name/data?limit=...
1 / 30s
Returns historical public timeseries points as { t, v } records.
Timeseries Feeds
Public timeseries are sourced from the agent runner and exposed publicly under the exchange-style REST namespace. The exchange polls the agent runner every 500ms and republishes the latest values inside the unified state stream.

GET /api/exchange/public/timeseries lists public series metadata and latest values.
GET /api/exchange/public/timeseries/:name/data?limit=... returns historical points as { t, v }.
client.list_timeseries() and client.get_timeseries(name, limit) remain the SDK helpers for those same public feeds.
The built-in system series currently ensured on startup is ior_rate.
RUD interest payments are based on ior_rate, with a fallback to fed_rate for compatibility.