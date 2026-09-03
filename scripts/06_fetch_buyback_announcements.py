"""
Fetch "Share Buy Back-On Market" announcements (category ANNC / sub ANNC13) from
SGX's public announcements API for a trailing lookback window, and merge any new
filings into the running history at history/buyback_history.csv.

Auth note: the announcements endpoint requires a short-lived token. SGX's own
frontend (sgx.com) obtains it by fetching a public CMS field ("qrValidator")
and ROT13-decoding it client-side -- the same trivial, publicly-computable step
every visitor's browser performs to load this public disclosure page. This
script reproduces that step; it does not bypass any access control on
non-public data.

Note on transport: api.sgx.com/announcements sits behind Akamai Bot Manager,
which scores requests partly on TLS/HTTP fingerprint. Python's requests/urllib3
is 403'd outright; system `curl` passes from a residential IP but is still
blocked from datacenter IPs (e.g. GitHub-hosted runners). We therefore use
curl_cffi with Chrome impersonation so the TLS/HTTP2 handshake matches a real
browser, prime the session by loading the announcements page first, and retry
on transient Akamai denials. If SGX's block is weighted toward IP reputation
rather than fingerprint this still won't pass from a datacenter -- set
SGX_FETCH_IMPERSONATE / run from a residential IP or proxy in that case.
"""
import datetime as dt
import os
import time
import urllib.parse

import pandas as pd
from curl_cffi import requests as cr

APPCONFIG_URL = "https://www.sgx.com/config/appconfig.json"
ANNOUNCEMENTS_PAGE_URL = "https://www.sgx.com/securities/company-announcements"
HISTORY_PATH = "../history/buyback_history.csv"  # history/ (unlike data/) is committed by the scheduled run

CAT = "ANNC"
SUB = "ANNC13"  # Share Buy Back-On Market
LOOKBACK_DAYS = 14  # re-fetch a trailing window each run so a missed/failed run day self-heals
PAGE_SIZE = 250

# curl_cffi browser profile to impersonate; override via env if a newer profile helps.
IMPERSONATE = os.environ.get("SGX_FETCH_IMPERSONATE", "chrome")
RETRIES = 4
BACKOFF_SECONDS = 5

HEADERS = {
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


def make_session():
    """A curl_cffi session that presents a real Chrome TLS/HTTP2 fingerprint, primed
    by loading the announcements page the way a browser would before its XHR calls."""
    s = cr.Session(impersonate=IMPERSONATE, timeout=30)
    try:
        r = s.get(ANNOUNCEMENTS_PAGE_URL)
        print(f"  primed session on {ANNOUNCEMENTS_PAGE_URL} -> HTTP {r.status_code}")
    except Exception as e:  # priming is best-effort; the real calls below still try
        print(f"  warning: could not prime session ({e!r})")
    return s


def get_json(session, url, extra_headers=None, what="request"):
    headers = dict(HEADERS, **(extra_headers or {}))
    last = None
    for attempt in range(1, RETRIES + 1):
        resp = session.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        last = resp
        snippet = resp.text[:200].replace("\n", " ")
        print(f"  {what}: HTTP {resp.status_code} on attempt {attempt}/{RETRIES}: {snippet}")
        if attempt < RETRIES:
            time.sleep(BACKOFF_SECONDS * attempt)
    raise RuntimeError(
        f"{what} failed after {RETRIES} attempts (last HTTP {last.status_code}). "
        f"Body: {last.text[:400]}"
    )


def get_endpoints(session):
    cfg = get_json(session, APPCONFIG_URL, what="appconfig")
    return cfg["endpoints"]["ANNOUNCEMENTS_API_URL"], cfg["endpoints"]["CMS_API_URL"], cfg["CMS_VERSION"]


def get_token(session, cms_api_url, cms_version):
    url = f"{cms_api_url}/?queryId={cms_version}:we_chat_qr_validator"
    data = get_json(session, url, what="cms token")
    return rot13(data["data"]["qrValidator"])


def fetch_buyback_announcements(session, announcements_api_url, token, period_start, period_end):
    params = {
        "cat": CAT,
        "sub": SUB,
        "periodstart": period_start.strftime("%Y%m%d_000000"),
        "periodend": period_end.strftime("%Y%m%d_235959"),
        "pagestart": 0,
        "pagesize": PAGE_SIZE,
    }
    url = f"{announcements_api_url}?{urllib.parse.urlencode(params)}"
    payload = get_json(session, url, extra_headers={"authorizationToken": token}, what="announcements")
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
    session = make_session()
    announcements_api_url, cms_api_url, cms_version = get_endpoints(session)
    token = get_token(session, cms_api_url, cms_version)

    today = dt.date.today()
    period_start = today - dt.timedelta(days=LOOKBACK_DAYS)
    items = fetch_buyback_announcements(session, announcements_api_url, token, period_start, today)
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
