151 Strategies → your competition
Every applicable strategy from Kakushadze & Serur, mapped to your exact exchange rules and ranked by probability of beating other teams
Applicable strategies
23
Avg win probability
61%
Asset types covered
5
Tier 1 (≥80%)
6
All (23)
Tier 1 — deploy now
Options
Spot / equities
Forex
Bonds
Prediction markets
Market-making (bid-ask spread capture)
Low complexity
Spot, Forex
Tier 1
88%
win prob
Why this gives you edge
§3.19 — The exchange has continuous limit-order matching. You post both sides of the book and collect the spread on every fill. Most beginner teams won't do this properly, giving you edge through sheer volume of small wins.

How to implement
Post bid at best_bid and ask at best_ask every tick. Size 1-2% of capital per side. Cancel and repost when the market moves. Use the timeseries to track your inventory drift and flatten when exposure grows.

Risk management
Inventory risk if price trends hard one way. Set max inventory = 5% of capital and auto-flatten.

Source: §3.19 Statistical arbitrage / market-making
Write me this bot ↗
Pairs trading (stat arb)
Medium complexity
Spot equities
Tier 1
85%
win prob
Why this gives you edge
§3.8 — If admins create multiple correlated spot assets, the spread between them mean-reverts. You short the expensive one and buy the cheap one. Pure market-neutral alpha — uncorrelated to every other team's directional bets.

How to implement
Track the price ratio of two assets. Compute rolling z-score of the spread (20-tick window). Enter when |z|>2, exit when |z|<0.5. Size inversely to recent volatility.

Risk management
Correlation breakdown. Only trade pairs that have traded together for ≥50 ticks.

Source: §3.8 Pairs trading
Write me this bot ↗
Prediction market mispricing
Low complexity
Prediction markets
Tier 1
83%
win prob
Why this gives you edge
§19 (event-driven analog) — Prediction shares must stay between $0.01–$0.99. If a market is at $0.20 but your probability estimate is 60%, expected value per share is $0.40. Most teams won't price these carefully.

How to implement
For each prediction market, form a probability estimate from any available timeseries data. If |market_price − your_estimate| > 0.15, trade the difference. Use Kelly criterion for sizing: f = (p - q/b) where b = payout ratio.

Risk management
Admin resolution is binary. Never put more than 8% of capital in any single prediction market.

Source: §19.5 Trading on economic announcements (analog)
Write me this bot ↗
Kelly criterion position sizing (meta-strategy)
Low complexity
All
Tier 1
82%
win prob
Why this gives you edge
Not a trading signal — it's an optimal bet sizing framework. Given your edge (expected return) and variance, Kelly tells you exactly what fraction of capital to allocate to maximize log wealth. This alone can outperform teams using flat sizing.

How to implement
f* = (μ - r) / σ² where μ=expected return, r=risk-free rate (ior_rate), σ²=variance of returns. Compute this for each open strategy. Scale down by 0.5× (half-Kelly) to reduce variance. Never exceed f* > 0.25.

Risk management
Kelly sizing requires accurate estimates of μ and σ. Overestimating edge → overbetting → ruin. Always use half-Kelly.

Source: §3.20 Alpha combos / position sizing
Write me this bot ↗
FX triangular arbitrage
Medium complexity
Forex pairs
Tier 1
81%
win prob
Why this gives you edge
§8.5 — If admins create 3+ FX pairs, a triangle may misprice. E.g. if EUR/USD × USD/GBP ≠ EUR/GBP you have riskless profit. Bots can check this in microseconds; humans cannot.

How to implement
For each triplet (A/B, B/C, A/C): compute implied A/C = A/B × B/C. If implied ≠ market A/C by > spread cost, trade all three legs simultaneously. Fire immediately — this arbitrage closes in seconds.

Risk management
All three legs must fill. Use IOC (immediate-or-cancel) orders. If one leg fails, cancel others instantly.

Source: §8.5 FX triangular arbitrage
Write me this bot ↗
Mean reversion — single asset
Low complexity
Spot, Forex
Tier 1
80%
win prob
Why this gives you edge
§3.9 — Admin-seeded assets tend to have house liquidity providing artificial mean reversion. Buy dips from the rolling mean, sell rips. Extremely robust in low-frequency comp environments.

How to implement
Compute 20-tick exponential moving average. Buy when price < EMA × 0.98. Sell when price > EMA × 1.02. Position size: 5% capital. Auto-close after 30 ticks if no reversion.

Risk management
Trending markets destroy mean-reversion strategies. Add a trend filter: skip signals when price is > 2 stddev from 100-tick mean.

Source: §3.9 Mean-reversion — single cluster
Write me this bot ↗
IOR rate carry (RUD interest farming)
Zero effort complexity
RUD cash
Tier 1
80%
win prob
Why this gives you edge
§17.3 Liquidity management — Your idle RUD earns from the ior_rate timeseries. This is free alpha. Every dollar sitting idle in bot capital earns interest. Most teams forget this.

How to implement
Keep a dedicated bot with high capital allocation that does nothing but hold RUD. Periodically read the ior_rate from the state stream and log your expected accrual. Only deploy capital when edge > ior_rate.

Risk management
Zero. This is the risk-free rate of the exchange. Use it as your hurdle rate for every other trade.

Source: §17.3 Liquidity management / cash carry
Write me this bot ↗
Statistical arb — multi-asset optimization
High complexity
Spot equities
76%
win prob
Why this gives you edge
§3.18 — When multiple spot assets exist, build a dollar-neutral portfolio: long assets with negative recent returns, short those with positive returns, weighted by inverse variance. Pure mean-reversion at scale.

How to implement
Every N ticks: compute returns for all assets. Z-score each return. Long assets with z < -1, short assets with z > 1. Weight by 1/variance. Keep dollar-neutral: sum(long weights) = sum(short weights).

Risk management
Correlated liquidation. This strategy can lose fast if ALL assets move the same direction. Monitor total portfolio beta.

Source: §3.18 Statistical arbitrage — optimization
Write me this bot ↗
Multi-factor portfolio construction
High complexity
Spot equities
75%
win prob
Why this gives you edge
§3.6 — Combine momentum, mean-reversion, and volume signals into a single composite score per asset. Diversified alpha is more stable than any single signal. This is what professional quant funds do.

How to implement
Score each asset on: (1) momentum z-score, (2) mean-reversion z-score, (3) spread relative to book. Normalize and combine with equal weights. Long top-third, short bottom-third. Reweight every 10 ticks.

Risk management
Complex to implement. Get individual signals working first, then combine. Test combined signal improves Sharpe before deploying.

Source: §3.6 Multifactor portfolio
Write me this bot ↗
Volatility risk premium (options)
High complexity
Options
74%
win prob
Why this gives you edge
§7.4 — Options are typically overpriced relative to realized volatility (the volatility risk premium). Sell options, collect premium, delta-hedge with the underlying spot asset.

How to implement
Identify options with implied vol > realized vol (compute realized vol from recent trades). Sell the option, buy/sell underlying to delta-hedge. P&L = collected premium minus hedging costs.

Risk management
Black swan events cause huge losses on short options. Never short options near binary resolution events.

Source: §7.4 Volatility risk premium
Write me this bot ↗
Sentiment / Bayes signal (if timeseries available)
High complexity
Prediction markets
73%
win prob
Why this gives you edge
§18.3 Naïve Bayes — If the exchange publishes timeseries data correlated with prediction outcomes, use Bayesian updating to adjust probability estimates. More accurate than static guesses.

How to implement
Collect timeseries values as features. Use a naïve Bayes classifier (sklearn.naive_bayes.BernoulliNB) trained on simulated outcomes. Update posterior as new ticks arrive. Trade when posterior diverges from market price by >15%.

Risk management
Model overfitting to small samples. Use wide confidence intervals early in the competition.

Source: §18.3 Sentiment analysis — naïve Bayes Bernoulli
Write me this bot ↗
Bond yield auction strategy
Medium complexity
Bonds
72%
win prob
Why this gives you edge
§5 Fixed Income — Bonds auction at $1000 par. If you model fair yield from the ior_rate timeseries, you can bid at the stop-out yield to capture above-market coupons paid in RUD.

How to implement
Monitor ior_rate to estimate fair yield. At auction, bid slightly above fair yield (you want to win). After winning, hold to collect coupon payments. Fair value = par / (1 + yield)^T

Risk management
If ior_rate rises after you win the bond, your coupon is below market. Hedge by keeping bond position < 15% of capital.

Source: §5.5–5.8 Bond strategies
Write me this bot ↗
Residual momentum (alpha signal)
High complexity
Spot equities
71%
win prob
Why this gives you edge
§3.7 — Compute residual returns after regressing out market-wide movement. Stocks with high residual momentum outperform. In a multi-asset exchange, this isolates asset-specific signals from correlated market moves.

How to implement
Regress each asset's returns on the equal-weighted market return (numpy.linalg.lstsq). The residual is idiosyncratic momentum. Long high-residual assets, short low-residual ones.

Risk management
Requires at least 5-10 assets to have signal. Don't use with fewer than 3 correlated spot assets.

Source: §3.7 Residual momentum
Write me this bot ↗
Carry trade (FX)
Medium complexity
Forex
70%
win prob
Why this gives you edge
§8.2 — Borrow in low-ior-rate currency, invest in high-ior-rate currency. In this exchange, IOR is paid on RUD. If Forex assets have different implicit rates, carry applies.

How to implement
Monitor ior_rate timeseries. Hold long positions in currencies implicitly earning above ior_rate, short those earning below. Net carry = rate differential × position size × time.

Risk management
Carry unwinds sharply in risk-off events. Keep total carry exposure < 20% of capital.

Source: §8.2 Carry trade
Write me this bot ↗
Bull call spread (options)
Medium complexity
Options
69%
win prob
Why this gives you edge
§2.6 — Buy call at lower strike K1, sell call at higher strike K2. Capped upside, capped downside. Better risk/reward than buying calls outright. Good when you expect moderate upside.

How to implement
Max profit = K2 - K1 - net_premium. Max loss = net_premium paid. Enter when you have a bullish view on the underlying within a defined range. Net premium should be < 40% of spread width.

Risk management
Defined risk strategy — your max loss is the premium paid. Great for beginners to options.

Source: §2.6 Bull call spread
Write me this bot ↗
Moving average crossover
Low complexity
Spot, Forex
68%
win prob
Why this gives you edge
§3.12 — Two moving averages (fast/slow). When fast crosses above slow, buy. When fast crosses below, sell. Simple to implement, captures medium-term trends in admin-driven price series.

How to implement
Fast EMA (5 ticks) and slow EMA (20 ticks). Signal on crossover. Enter 3% of capital. Exit on reverse crossover or after 50 ticks. Works best on assets with clear admin-seeded trends.

Risk management
Whipsawing in choppy markets. Add a minimum momentum threshold: only trade if |fast_ema - slow_ema| > 1% of price.

Source: §3.12 Two moving averages
Write me this bot ↗
Sector/asset rotation (momentum)
Medium complexity
Spot, Forex
67%
win prob
Why this gives you edge
§4.1 Sector momentum rotation — Rotate capital into the best-performing asset over recent ticks, exit the worst. Simple but effective when multiple assets exist with different momentum profiles.

How to implement
Every 20 ticks: rank all assets by 20-tick return. Allocate 30% of capital to top performer, 0% to bottom, proportional in between. Rebalance when rank changes.

Risk management
Transaction costs from frequent rebalancing. Only rotate when top-1 and top-2 rankings actually swap.

Source: §4.1 Sector momentum rotation
Write me this bot ↗
Protective put (downside hedge)
Medium complexity
Options + Spot
66%
win prob
Why this gives you edge
§2.4 — Buy a put option to hedge a spot position. Limits your downside to the strike price while keeping upside open. Use on large spot positions to protect against liquidation near the bailout threshold.

How to implement
For each major spot position, buy an OTM put at 90% of current price. Cost = option premium. This is insurance — buy it when you're up and want to protect gains, not when you're already down.

Risk management
Premium decay. Puts lose value over time. Only buy puts on positions worth protecting (>10% of capital).

Source: §2.4 Protective put
Write me this bot ↗
Momentum — single asset
Low complexity
Spot, Forex
65%
win prob
Why this gives you edge
§3.1 Price-momentum — Assets that have moved up over recent ticks tend to keep moving. Especially powerful if you're one of the first teams to enter a trending admin-seeded asset.

How to implement
Compute 10-tick return. If return > 1.5%, buy. If return < -1.5%, short. Size 4% of capital. Hard stop-loss at 2% loss. Exit after 20 ticks or on signal reversal.

Risk management
Momentum reversal. Never hold momentum positions overnight in a competition — take profit at +3%.

Source: §3.1 Price-momentum
Write me this bot ↗
Collar (protect large position)
Medium complexity
Options + Spot
65%
win prob
Why this gives you edge
§2.53 — Buy a put AND sell a call on an existing spot position. The call premium partially funds the put. Limits both upside and downside. Use when holding large profitable spot positions near competition end.

How to implement
For large spot holding: buy put at 95% of current price, sell call at 105%. Net cost should be near zero (zero-cost collar). You're protected from >5% drawdown while capping your gain at +5%.

Risk management
You give up upside above the call strike. Only use when protecting profits, not when building them.

Source: §2.53 Collar
Write me this bot ↗
Straddle — earnings-style event play
High complexity
Options
64%
win prob
Why this gives you edge
§2.22 Long straddle — Buy both a call and a put at the same strike. Profits from large moves in either direction. Use before admin-triggered events like bond maturities or prediction market resolutions.

How to implement
Buy ATM call + ATM put before a known event. Position size: 2-3% of capital. Max loss = both premiums. Break-even = strike ± combined premium. Close immediately after the event.

Risk management
If price doesn't move much, you lose both premiums. Only use when you expect high volatility.

Source: §2.22 Long straddle
Write me this bot ↗
Support and resistance levels
Low complexity
Spot, Forex
63%
win prob
Why this gives you edge
§3.14 — Track price levels where the asset repeatedly bounced or reversed. Buy near support, sell near resistance. Even in a simulated exchange, order book clustering creates real support/resistance.

How to implement
Track local minima and maxima over rolling 50-tick window. Support = avg of 3 most recent local lows. Resistance = avg of 3 most recent local highs. Buy within 0.5% of support, sell within 0.5% of resistance.

Risk management
Support/resistance breaks are violent. Always set stop-loss 1% below support when buying.

Source: §3.14 Support and resistance
Write me this bot ↗
Iron condor (range-bound options)
High complexity
Options
62%
win prob
Why this gives you edge
§2.50 — Sell an OTM call and OTM put, buy further OTM call and put for protection. Profits when price stays in a range. Admin-seeded assets with mean-reverting behavior are perfect candidates.

How to implement
Sell call at K3, buy call at K4 (K4>K3). Sell put at K2, buy put at K1 (K1

Risk management
Large moves in either direction. Size at 3-4% of capital per condor. Close early if loss reaches 50% of max profit.

Source: §2.50 Long iron condor
Write me this bot ↗