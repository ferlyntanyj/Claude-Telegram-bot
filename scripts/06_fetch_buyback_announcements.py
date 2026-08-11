"""
Fetch "Share Buy Back-On Market" announcements (category ANNC / sub ANNC13) from
SGX's public announcements API for a trailing lookback window, and merge any new
filings into the running history at data/buyback_history.csv.

Auth note: the announcements endpoint requires a short-lived token. SGX's own
frontend (sgx.com) obtains it by fetching a public CMS field ("qrValidator")
and ROT13-decoding it client-side -- the same trivial, publicly-computable step
every visitor's browser performs to load this public disclosure page. This
script reproduces that step; it does not bypass any access control on
non-public data.

Note on transport: the announcements endpoint sits behind bot-fingerprinting
(Akamai) that 403s Python's `requests`/urllib3 regardless of headers, but
accepts an identically-headered `curl` request. The listing call therefore
shells out to `curl`; the config/token calls (unaffected) use `requests`.
"""
import datetime as dt
import json
import subprocess
import urllib.parse

import pandas as pd
import requests

APPCONFIG_URL = "https://www.sgx.com/config/appconfig.json"
ANNOUNCEMENTS_PAGE_URL = "https://www.sgx.com/stock-exchange/company-announcements"
HISTORY_PATH = "../history/buyback_history.csv"  # history/ (unlike data/) is committed by the scheduled run

CAT = "ANNC"
SUB = "ANNC13"  # Share Buy Back-On Market
LOOKBACK_DAYS = 14  # re-fetch a trailing window each run so a missed/failed run day self-heals
PAGE_SIZE = 250

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ANNOUNCEMENTS_PAGE_URL,
    "Origin": "https://www.sgx.com",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-fetch-dest": "empty",
}


def rot13(s):
    return s.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm",
    ))


def get_endpoints():
    resp = requests.get(APPCONFIG_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    cfg = resp.json()
    return cfg["endpoints"]["ANNOUNCEMENTS_API_URL"], cfg["endpoints"]["CMS_API_URL"], cfg["CMS_VERSION"]


def get_token(cms_api_url, cms_version):
    url = f"{cms_api_url}/?queryId={cms_version}:we_chat_qr_validator"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return rot13(resp.json()["data"]["qrValidator"])


def curl_get_json(url, headers, timeout=30):
    cmd = ["curl", "-s", "--compressed", "--fail-with-body"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd += [url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}) for {url}: {result.stdout[:300] or result.stderr[:300]}")
    return json.loads(result.stdout)


def fetch_buyback_announcements(announcements_api_url, token, period_start, period_end):
    headers = dict(HEADERS, authorizationToken=token)
    params = {
        "cat": CAT,
        "sub": SUB,
        "periodstart": period_start.strftime("%Y%m%d_000000"),
        "periodend": period_end.strftime("%Y%m%d_235959"),
        "pagestart": 0,
        "pagesize": PAGE_SIZE,
    }
    url = f"{announcements_api_url}?{urllib.parse.urlencode(params)}"
    payload = curl_get_json(url, headers)
    items = payload.get("data") or []
    if payload["meta"]["totalItems"] > PAGE_SIZE:
        print(f"  warning: {payload['meta']['totalItems']} items exceeds page size {PAGE_SIZE}; some may be missing")
    return items


def parse_rows(items):
    rows = []
    for it in items:
        issuers = it.get("issuers") or []
        stock_code = issuers[0]["stock_code"] if issuers else None
        issuer_name = it.get("issuer_name") or (issuers[0]["issuer_name"] if issuers else None)
        rows.append({
            "id": it.get("id"),
            "stock_code": stock_code,
            "issuer_name": issuer_name,
            "buyback_date": dt.datetime.strptime(it["submission_date"], "%Y%m%d").date().isoformat(),
            "title": it.get("title"),
            "url": it.get("url"),
        })
    return rows


def main():
    announcements_api_url, cms_api_url, cms_version = get_endpoints()
    token = get_token(cms_api_url, cms_version)

    today = dt.date.today()
    period_start = today - dt.timedelta(days=LOOKBACK_DAYS)
    items = fetch_buyback_announcements(announcements_api_url, token, period_start, today)
    fetched = pd.DataFrame(parse_rows(items))
    print(f"Fetched {len(fetched)} buy-back notices from {period_start} to {today}")

    try:
        history = pd.read_csv(HISTORY_PATH, dtype=str)
    except FileNotFoundError:
        history = pd.DataFrame(columns=["id", "stock_code", "issuer_name", "buyback_date", "title", "url"])

    if fetched.empty:
        new_rows = fetched
    else:
        new_rows = fetched[~fetched["id"].isin(history["id"])]

    if not new_rows.empty:
        history = pd.concat([history, new_rows], ignore_index=True)
        history = history.drop_duplicates(subset="id").sort_values(["buyback_date", "stock_code"])
        history.to_csv(HISTORY_PATH, index=False)

    print(f"{len(new_rows)} new notice(s) added to history. History now has {len(history)} total row(s).")


if __name__ == "__main__":
    main()
