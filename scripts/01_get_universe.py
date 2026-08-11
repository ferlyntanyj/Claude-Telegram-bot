"""
Fetch the full list of SGX mainboard + Catalist listed stocks from SGX's
public securities API and save as the screening universe.
"""
import json
import requests
import pandas as pd

SGX_API = "https://api.sgx.com/securities/v1.1"
OUT_PATH = "../data/universe.csv"


def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(SGX_API, headers=headers, timeout=30)
    resp.raise_for_status()
    prices = resp.json()["data"]["prices"]

    rows = []
    for p in prices:
        if p.get("type") != "stocks":
            continue
        market = p.get("m")
        if market not in ("MAINBOARD", "CATALIST"):
            continue
        code = p.get("nc")
        name = p.get("n")
        if not code or not name:
            continue
        rows.append({
            "sgx_code": code,
            "name": name,
            "market": market,
            "yahoo_ticker": f"{code}.SI",
        })

    df = pd.DataFrame(rows).drop_duplicates(subset="sgx_code").sort_values("sgx_code")
    df.to_csv(OUT_PATH, index=False)
    print(f"Saved {len(df)} tickers to {OUT_PATH}")
    print(df["market"].value_counts())


if __name__ == "__main__":
    main()
