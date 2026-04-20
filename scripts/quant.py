import time
from knight_trader import ExchangeClient

SYMBOL = "SPOTDEMO"  # Replace with a real tradable symbol on competition day.

def run():
    client = ExchangeClient()
    resting_bid = None
    resting_ask = None
    next_refresh = 0.0

    for state in client.stream_state():
        try:
            if state.get("competition_state") != "live":
                continue
            if time.monotonic() < next_refresh:
                continue

            book = state.get("book", {}).get(SYMBOL, {})
            bids = sorted((float(p) for p in book.get("bids", {}).keys()), reverse=True)
            asks = sorted(float(p) for p in book.get("asks", {}).keys())

            if not bids or not asks:
                continue

            best_bid = bids[0]
            best_ask = asks[0]
            spread = best_ask - best_bid

            if spread < 0.05:
                continue

            if resting_bid:
                client.cancel(resting_bid)
            if resting_ask:
                client.cancel(resting_ask)

            bid_px = round(best_bid + 0.01, 4)
            ask_px = round(best_ask - 0.01, 4)

            resting_bid = client.buy(SYMBOL, bid_px, 1.0)
            resting_ask = client.sell(SYMBOL, ask_px, 1.0)
            next_refresh = time.monotonic() + 0.5
        except Exception as exc:
            print(f"bot error: {exc}")
            time.sleep(1.0)

if __name__ == "__main__":
    run()