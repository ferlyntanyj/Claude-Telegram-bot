"""
Shared config for the weekly earnings calendar alert (earnings_calendar.py /
send_earnings_calendar_telegram.py).

SOX_CONSTITUENTS is a hand-maintained seed list -- same approach this repo
already uses for the Taiwan/Vietnam seed lists in global_watchlist_build.py
-- because the PHLX Semiconductor Sector Index has no scrapable constituents
table on Wikipedia (its page is a navbox, not a holdings table; verified
2026-10-27). Sourced instead from Nasdaq's own factsheet
(nasdaq.com/docs/global-indexes/SOX-<month>-<year>), data as of 2026-08-03,
the most recent one published at the time this list was built. SOX
rebalances quarterly -- re-verify against a fresh factsheet at
https://www.nasdaq.com/solutions/global-indexes/sox if holdings look stale.
S&P 500 constituents are NOT seeded here -- they're fetched live each run
via global_watchlist_build.fetch_sp500() (Wikipedia), already used elsewhere
in this repo.
"""

# (ticker, company name), 30 names, as of the 2026-08-03 Nasdaq SOX factsheet.
SOX_CONSTITUENTS = [
    ("AMD", "Advanced Micro Devices"),
    ("ADI", "Analog Devices"),
    ("AMAT", "Applied Materials"),
    ("ARM", "Arm Holdings"),
    ("ASML", "ASML Holding"),
    ("ALAB", "Astera Labs"),
    ("AVGO", "Broadcom"),
    ("COHR", "Coherent Corp"),
    ("CRDO", "Credo Technology Group"),
    ("ENTG", "Entegris"),
    ("GFS", "GlobalFoundries"),
    ("INTC", "Intel"),
    ("KLAC", "KLA Corp"),
    ("LRCX", "Lam Research"),
    ("MTSI", "MACOM Technology Solutions"),
    ("MRVL", "Marvell Technology"),
    ("MCHP", "Microchip Technology"),
    ("MU", "Micron Technology"),
    ("MPWR", "Monolithic Power Systems"),
    ("NVMI", "Nova Ltd"),
    ("NVDA", "Nvidia"),
    ("NXPI", "NXP Semiconductors"),
    ("ON", "ON Semiconductor"),
    ("QRVO", "Qorvo"),
    ("QCOM", "Qualcomm"),
    ("RMBS", "Rambus"),
    ("SWKS", "Skyworks Solutions"),
    ("TSM", "Taiwan Semiconductor Manufacturing"),
    ("TER", "Teradyne"),
    ("TXN", "Texas Instruments"),
]

OUT_CSV_PATH = "../output/earnings_calendar.csv"

# Only the N largest S&P 500 names by market cap are included (full index is
# too noisy for a weekly alert). SOX is unaffected -- all ~30 constituents
# are always included regardless of this cap.
SP500_TOP_N = 100
