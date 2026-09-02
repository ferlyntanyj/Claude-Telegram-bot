"""
Fetch daily OHLCV history (2024-01-01 to today) for every ticker in the
SGX universe via yfinance, in batches. Saves one long-format parquet file:
columns = [date, ticker, close, volume]
"""
import os
import time
import pandas as pd
import yfinance as yf

UNIVERSE_PATH = "../data/universe.csv"
OUT_PATH = "../data/history.parquet"
START = "2024-01-01"
BATCH_SIZE = 40


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    universe = pd.read_csv(UNIVERSE_PATH)
    tickers = universe["yahoo_ticker"].tolist()

    all_frames = []
    failed = []

    for bi, batch in enumerate(chunked(tickers, BATCH_SIZE)):
        print(f"Batch {bi + 1}/{(len(tickers) - 1) // BATCH_SIZE + 1}: {len(batch)} tickers")
        try:
            data = yf.download(
                tickers=batch,
                start=START,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception as e:
            print(f"  batch failed: {e}")
            failed.extend(batch)
            continue

        for t in batch:
            try:
                if len(batch) == 1:
                    sub = data
                else:
                    sub = data[t]
                sub = sub[["Close", "Volume"]].dropna(how="all")
                if sub.empty:
                    failed.append(t)
                    continue
                sub = sub.reset_index().rename(columns={"Date": "date", "Close": "close", "Volume": "volume"})
                sub["ticker"] = t
                all_frames.append(sub[["date", "ticker", "close", "volume"]])
            except Exception:
                failed.append(t)

        time.sleep(1)

    history = pd.concat(all_frames, ignore_index=True)
    history.to_parquet(OUT_PATH, index=False)
    print(f"Saved {len(history)} rows for {history['ticker'].nunique()} tickers to {OUT_PATH}")
    if failed:
        print(f"Failed/empty: {len(failed)} tickers")
        with open("../data/failed_tickers.txt", "w") as f:
            f.write("\n".join(failed))


if __name__ == "__main__":
    main()
