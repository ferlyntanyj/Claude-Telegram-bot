"""
Build a per-company index of share buy-backs from history/buyback_history.csv --
one row per company with its most recent buy-back date, so you can look up a
single company without scrolling the full chronological log (which mixes every
company's every filing together).

Each company name links directly to its most recent buy-back announcement on
SGX for one-click verification.
"""
import datetime as dt
import pandas as pd

HISTORY_PATH = "../history/buyback_history.csv"
OUT_MD_PATH = "../output/buyback_company_summary.md"
OUT_CSV_PATH = "../output/buyback_company_summary.csv"


def main():
    history = pd.read_csv(HISTORY_PATH, dtype=str)
    history["buyback_date"] = pd.to_datetime(history["buyback_date"]).dt.date

    today = dt.date.today()

    rows = []
    for stock_code, g in history.groupby("stock_code"):
        g = g.sort_values("buyback_date")
        latest = g.iloc[-1]
        rows.append({
            "stock_code": stock_code,
            "issuer_name": latest["issuer_name"],
            "last_buyback_date": latest["buyback_date"].isoformat(),
            "days_ago": (today - latest["buyback_date"]).days,
            "buybacks_tracked": len(g),
            "tracked_since": g.iloc[0]["buyback_date"].isoformat(),
            "url": latest["url"],
        })

    summary = pd.DataFrame(rows).sort_values("issuer_name")
    summary.to_csv(OUT_CSV_PATH, index=False)

    lines = [
        "# SGX Share Buy-Back — Per-Company Summary",
        "",
        f"**As of:** {today.isoformat()}",
        f"**Companies tracked:** {len(summary)}",
        "",
        "One row per company, alphabetical -- use Ctrl+F to jump to a specific company. "
        "\"Days ago\" is relative to today, not to when this page was last generated.",
        "",
        "| Company | Stock | Last buy-back date | Days ago | Buy-backs tracked | Tracked since |",
        "|---|---|---|---|---|---|",
    ]
    for _, r in summary.iterrows():
        company_link = f"[{r['issuer_name']}]({r['url']})" if pd.notna(r["url"]) else r["issuer_name"]
        lines.append(
            f"| {company_link} | {r['stock_code']} | {r['last_buyback_date']} | {r['days_ago']} | "
            f"{r['buybacks_tracked']} | {r['tracked_since']} |"
        )
    lines.append("")
    lines.append(
        "Note: \"tracked since\" reflects when this pipeline started logging, not the company's actual "
        "first-ever buy-back -- history accumulates day by day from when this alert was set up."
    )

    summary_text = "\n".join(lines)
    with open(OUT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(summary_text)

    print(f"Saved {len(summary)}-company summary to {OUT_MD_PATH} and {OUT_CSV_PATH}")


if __name__ == "__main__":
    main()
