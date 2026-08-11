"""
Compare this run's Top 10 against the most recently saved snapshot, report
new entrants / drop-offs, then save this run's snapshot for next week.
"""
import glob
import os
import pandas as pd

TOP10_PATH = "../output/liquidity_momentum_screener_top10.csv"
HISTORY_DIR = "../history"
SUMMARY_PATH = "../output/weekly_summary.md"


def main():
    os.makedirs(HISTORY_DIR, exist_ok=True)

    current = pd.read_csv(TOP10_PATH)
    current["rank"] = range(1, len(current) + 1)
    run_date = current["recent_end"].max()  # last trading date in this run's data

    snapshot_path = os.path.join(HISTORY_DIR, f"top10_{run_date}.csv")

    prior_files = sorted(glob.glob(os.path.join(HISTORY_DIR, "top10_*.csv")))
    prior_files = [f for f in prior_files if os.path.basename(f) != os.path.basename(snapshot_path)]

    lines = [f"# SGX Liquidity Momentum Screener — Weekly Summary", "", f"**Run date (latest trading day):** {run_date}", ""]

    if not prior_files:
        lines.append("First run — no prior snapshot to compare against. This is the baseline.")
        lines.append("")
        lines.append("## Current Top 10")
        for _, row in current.iterrows():
            lines.append(f"{row['rank']}. **{row['yahoo_ticker']}** ({row['name']}) — {row['surge_ratio']:.2f}x")
    else:
        prior_path = prior_files[-1]
        prior_date = os.path.basename(prior_path).replace("top10_", "").replace(".csv", "")
        prior = pd.read_csv(prior_path)

        cur_tickers = set(current["yahoo_ticker"])
        prior_tickers = set(prior["yahoo_ticker"])

        new_entrants = cur_tickers - prior_tickers
        dropped = prior_tickers - cur_tickers
        held = cur_tickers & prior_tickers

        lines.append(f"**Compared against:** {prior_date}")
        lines.append("")

        lines.append(f"## New to the Top 10 ({len(new_entrants)})")
        if new_entrants:
            for _, row in current[current["yahoo_ticker"].isin(new_entrants)].iterrows():
                lines.append(f"- **{row['yahoo_ticker']}** ({row['name']}) — entered at #{row['rank']}, {row['surge_ratio']:.2f}x surge")
        else:
            lines.append("- None")
        lines.append("")

        lines.append(f"## Dropped off the Top 10 ({len(dropped)})")
        if dropped:
            for _, row in prior[prior["yahoo_ticker"].isin(dropped)].iterrows():
                lines.append(f"- **{row['yahoo_ticker']}** ({row['name']}) — was #{row['rank']} last week")
        else:
            lines.append("- None")
        lines.append("")

        if held:
            lines.append(f"## Still in Top 10 ({len(held)})")
            prior_rank = dict(zip(prior["yahoo_ticker"], prior["rank"]))
            for _, row in current[current["yahoo_ticker"].isin(held)].iterrows():
                prev_r = prior_rank.get(row["yahoo_ticker"])
                delta = prev_r - row["rank"]
                arrow = f"(+{delta})" if delta > 0 else (f"({delta})" if delta < 0 else "(=)")
                lines.append(f"- **{row['yahoo_ticker']}** ({row['name']}) — #{row['rank']} {arrow}, {row['surge_ratio']:.2f}x")
            lines.append("")

    summary_text = "\n".join(lines)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write(summary_text)

    current.to_csv(snapshot_path, index=False)

    print(summary_text)
    print(f"\nSaved snapshot: {snapshot_path}")


if __name__ == "__main__":
    main()
