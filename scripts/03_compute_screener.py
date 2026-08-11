"""
Compute the liquidity momentum screener:
  ADTV_2024   = mean(close * volume) over all 2024 trading days
  ADTV_recent = mean(close * volume) over the trailing 5 trading days
  surge_ratio = ADTV_recent / ADTV_2024

Outputs the full ranked table and prints/saves the top 10.
"""
import pandas as pd

HISTORY_PATH = "../data/history.parquet"
UNIVERSE_PATH = "../data/universe.csv"
OUT_PATH = "../output/liquidity_momentum_screener.csv"
OUT_PATH_FILTERED = "../output/liquidity_momentum_screener_top10.csv"
TOP_N = 10
RECENT_WINDOW = 5
MIN_2024_TRADING_DAYS = 100  # require reasonably complete 2024 history to avoid noisy ratios
MIN_ADTV_2024_SGD = 100_000  # liquidity floor: excludes near-untraded names where a tiny base
                              # inflates the ratio off noise rather than genuine momentum


def main():
    hist = pd.read_parquet(HISTORY_PATH)
    hist["date"] = pd.to_datetime(hist["date"])
    hist = hist.dropna(subset=["close", "volume"])
    hist = hist[(hist["close"] > 0) & (hist["volume"] >= 0)]
    hist["traded_value"] = hist["close"] * hist["volume"]

    universe = pd.read_csv(UNIVERSE_PATH)
    name_map = dict(zip(universe["yahoo_ticker"], universe["name"]))
    market_map = dict(zip(universe["yahoo_ticker"], universe["market"]))

    hist_2024 = hist[(hist["date"] >= "2024-01-01") & (hist["date"] <= "2024-12-31")]
    adtv_2024 = hist_2024.groupby("ticker").agg(
        adtv_2024=("traded_value", "mean"),
        n_days_2024=("traded_value", "count"),
    )

    latest_date = hist["date"].max()
    print(f"Latest trading date in data: {latest_date.date()}")

    recent_rows = []
    for ticker, g in hist.groupby("ticker"):
        g = g.sort_values("date").tail(RECENT_WINDOW)
        if len(g) < RECENT_WINDOW:
            continue
        recent_rows.append({
            "ticker": ticker,
            "adtv_recent": g["traded_value"].mean(),
            "recent_start": g["date"].min(),
            "recent_end": g["date"].max(),
            "n_days_recent": len(g),
        })
    adtv_recent = pd.DataFrame(recent_rows).set_index("ticker")

    df = adtv_2024.join(adtv_recent, how="inner")
    df = df[df["n_days_2024"] >= MIN_2024_TRADING_DAYS]
    df = df[df["adtv_2024"] > 0]

    df["surge_ratio"] = df["adtv_recent"] / df["adtv_2024"]
    df["name"] = df.index.map(name_map)
    df["market"] = df.index.map(market_map)

    df = df.reset_index().rename(columns={"ticker": "yahoo_ticker"})
    df = df[[
        "yahoo_ticker", "name", "market", "surge_ratio",
        "adtv_recent", "adtv_2024", "n_days_2024",
        "recent_start", "recent_end",
    ]]
    df = df.sort_values("surge_ratio", ascending=False)

    df.to_csv(OUT_PATH, index=False)
    print(f"Saved full ranked screener ({len(df)} tickers) to {OUT_PATH}")

    df_liquid = df[df["adtv_2024"] >= MIN_ADTV_2024_SGD].copy()
    df_liquid.to_csv(OUT_PATH_FILTERED.replace("_top10", "_liquid"), index=False)
    print(f"{len(df_liquid)} tickers pass the SGD {MIN_ADTV_2024_SGD:,.0f}/day 2024 ADTV liquidity floor")

    top10 = df_liquid.head(TOP_N).copy()
    top10.to_csv(OUT_PATH_FILTERED, index=False)
    top10["adtv_recent_SGD"] = top10["adtv_recent"].map(lambda x: f"{x:,.0f}")
    top10["adtv_2024_SGD"] = top10["adtv_2024"].map(lambda x: f"{x:,.0f}")
    top10["surge_ratio_x"] = top10["surge_ratio"].map(lambda x: f"{x:.2f}x")

    print("\nTop 10 by ADTV surge ratio (trailing 5D ADTV / 2024 ADTV):")
    print(top10[["yahoo_ticker", "name", "market", "surge_ratio_x", "adtv_recent_SGD", "adtv_2024_SGD"]].to_string(index=False))


if __name__ == "__main__":
    main()
