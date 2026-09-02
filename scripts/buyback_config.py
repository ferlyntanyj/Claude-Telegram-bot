"""Shared tuning knobs for the daily share buy-back alert (scripts 07 + delivery)."""

# Drop companies whose previous buy-back (before the latest trading day) was this
# many days ago or fewer -- i.e. routine daily / near-daily programmes. Companies
# with no prior buy-back on record are always kept. Set to 0 to disable the
# filter and list every company that filed on the latest trading day.
EXCLUDE_IF_PRIOR_BUYBACK_WITHIN_DAYS = 5
