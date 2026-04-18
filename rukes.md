Competitor bots are uploaded as a single Python file and mounted into an isolated Docker container.
The current guaranteed runtime is Python 3.9 with the bundled knight_trader SDK and the approved quantitative libraries numpy, pandas, scipy, scikit-learn, statsmodels, and ta-lib.
Upload size is capped at 256 KB per bot file.
Each bot container is started with a 256 MB memory limit and a 0.25 CPU limit.
Deployed bots receive BOT_ID and EXCHANGE_URL as environment variables. ExchangeClient() uses those automatically.
There is no separate public bot API key. The bot ID injected as BOT_ID is the bot credential used by the SDK.
Bots are uploaded in a paused state and must be started from the dashboard.
Trading Mechanics
Markets use continuous limit-order matching with strict price-time priority.
Buy orders lock the bot's allocated RUD capital.
Filled inventory belongs to the bot that acquired it.
Naked shorting is allowed, and uncovered short exposure is charged at 1.5x market value.
Self-match prevention is active. Resting orders from the same bot on the other side of the market are cancelled before a self-trade can occur.
Prediction markets can only trade between $0.01 and $0.99 per share.
The exchange can be globally halted by admins. During a halt, new orders, cancels, and auction bids are rejected.

Market Data & Latency

Market Data & Latency
Bots connect to authenticated /ws/state through the SDK and receive a locally rebuilt market state.
The live state includes tick, competition state, full order books, recent trades, and timeseries reconstructed from one snapshot plus incremental deltas.
The SDK blocks place_order() until it receives a private order-status delta for the generated client_order_id.
The SDK uses a zero-backlog queue for stream_state(), so slow bots can skip intermediate states and only process the newest reconstructed snapshot.

Assets
RUD: The internal cash asset. It is not tradable and is used for balances, payouts, bot capital allocation, scoring, and bailout resets.
Spot / Equities: Standard spot-style instruments with their own order books. These are created by admins and usually seeded to the House wallet for liquidity.
Forex: Implemented with the same matching model as spot assets, but displayed as FX-style instruments in the live view.
Bonds: Admin-created bond issues are auctioned at $1000 par per unit. The stop-out yield becomes the coupon rate and coupon payments are credited in RUD. Bond maturity and when-issued conversion are admin-managed lifecycle events in the current setup.
Options: Cash-settled derivatives with an underlying symbol, strike price, and call/put type. They trade as standalone assets and settle to intrinsic value on expiry or admin resolution.
Prediction Markets: YES-share contracts. If the market resolves YES, each long share pays $1 in RUD. If it resolves NO, the long position goes to zero and PnL is realized through prior trading.

Capital, Scoring & Bailouts
Leaderboard equity is computed from team RUD plus per-bot inventory marked at last traded prices, plus total bot capital, minus bailout penalties.
Assets with no recorded trade history can contribute zero to leaderboard valuation until they trade.
Team gross exposure is capped by the sum of bot capital allocations, using last traded prices for held assets.
RUD balances earn periodic Interest on Reserves from the ior_rate timeseries.
If a team's total equity falls to $50,000 or lower, the exchange triggers an automatic bailout.
A bailout pauses all team bots, wipes non-RUD positions, resets RUD to $1,000,000, and permanently adds a $1,500,000 leaderboard penalty.

Operational Notes
The public site exposes leaderboard, public book, public trades, public tape, and public timeseries data.
Competitor bot support is centered on the bundled SDK and websocket state stream rather than public REST polling.
Admins can create assets, toggle tradability, clear books, resolve markets, resolve options, convert when-issued assets, and mature bonds.

Penalties & Disqualification
Bots that exceed container limits or crash are not automatically repaired by the platform.
Attempting to bypass auth, tamper with balances, abuse infrastructure, or interfere with other teams is grounds for immediate disqualification.
Competitors are responsible for managing their own bot code, open orders, and capital usage during the event.

How do bots authenticate?
Deployed bots receive BOT_ID and EXCHANGE_URL in their container environment. The bundled ExchangeClient uses those automatically for trading.

How is the leaderboard calculated?
Total equity is team RUD plus per-bot inventory marked at last traded prices, plus total bot capital, minus any bailout penalties. If an asset has never traded, it may still mark at zero.

Can we short assets?
Yes. Naked shorting is allowed. Inventory is per bot, but exposure is enforced at the team level, and uncovered short exposure is charged at 1.5x market value.
Is the websocket stream JSON?
The official competitor stream is protobuf over /ws/state. The bundled Python SDK handles the snapshot-plus-delta protocol, local state rebuild, and reconnect behavior for you.
Where do public data feeds come from?
Public timeseries are sourced from the agent runner, exposed at /api/exchange/public/timeseries, and polled into the exchange state stream. The built-in system feed is ior_rate.
Do public REST endpoints replace the SDK?
No. They are useful for dashboards, analytics, and low-frequency tooling. For live bot logic, use the SDK and /ws/state.


