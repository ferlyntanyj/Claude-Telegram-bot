"""
Build the daily share buy-back alert from data/buyback_history.csv (populated by
06_fetch_buyback_announcements.py).

Flags every company that filed a buy-back notice on the most recent trading day
present in the history, and for each one reports the prior buy-back date on
record (before that day) and how many days elapsed between the two -- i.e.
whether this is a routine, ongoing daily programme or a resumption after a gap.
"""
import datetime as dt
import pandas as pd

HISTORY_PATH = "../history/buyback_history.csv"
OUT_MD_PATH = "../output/buyback_alert.md"
OUT_CSV_PATH = "../output/buyback_alert.csv"


def main():
    history = pd.read_csv(HISTORY_PATH, dtype=str)
    history["buyback_date"] = pd.to_datetime(history["buyback_date"]).dt.date

    today = dt.date.today()
    latest_date = history["buyback_date"].max()

    todays = history[history["buyback_date"] == latest_date].drop_duplicates(subset="stock_code")

    rows = []
    for _, r in todays.iterrows():
        prior = history[
            (history["stock_code"] == r["stock_code"]) & (history["buyback_date"] < latest_date)
        ]
        if prior.empty:
            prior_date = None
            days_since_prior = None
        else:
            prior_date = prior["buyback_date"].max()
            days_since_prior = (latest_date - prior_date).days
        rows.append({
            "stock_code": r["stock_code"],
            "issuer_name": r["issuer_name"],
            "last_buyback_date": latest_date.isoformat(),
            "days_since_run_date": (today - latest_date).days,
            "prior_buyback_date": prior_date.isoformat() if prior_date else None,
            "days_since_prior_buyback": days_since_prior,
            "url": r["url"],
        })

    alert = pd.DataFrame(rows).sort_values(
        by=["days_since_prior_buyback", "issuer_name"], ascending=[False, True], na_position="first"
    )
    alert.to_csv(OUT_CSV_PATH, index=False)

    lines = [
        "# SGX Share Buy-Back Alert",
        "",
        f"**Run date:** {today.isoformat()}",
        f"**Latest trading day with buy-back filings:** {latest_date.isoformat()} "
        f"({(today - latest_date).days} day(s) ago)",
        "",
    ]

    if alert.empty:
        lines.append("No share buy-back notices found on the latest trading day.")
    else:
        lines.append(f"## Companies that filed a share buy-back on {latest_date.isoformat()} ({len(alert)})")
        lines.append("")
        lines.append("| Stock | Company | Prior buy-back date | Days since prior buy-back |")
        lines.append("|---|---|---|---|")
        for _, r in alert.iterrows():
            prior_str = r["prior_buyback_date"] if pd.notna(r["prior_buyback_date"]) else "no prior record"
            gap_str = f"{int(r['days_since_prior_buyback'])}" if pd.notna(r["days_since_prior_buyback"]) else "first seen"
            lines.append(f"| {r['stock_code']} | {r['issuer_name']} | {prior_str} | {gap_str} |")

    summary_text = "\n".join(lines)
    with open(OUT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(summary_text)

    print(summary_text)
    print(f"\nSaved {OUT_MD_PATH} and {OUT_CSV_PATH}")


if __name__ == "__main__":
    main()
