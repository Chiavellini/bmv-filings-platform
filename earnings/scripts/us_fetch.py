#!/usr/bin/env python3
"""us_fetch.py — S&P 500 campaign data: constituents, earnings events, prices.

All via the TLS-impersonated client. Frozen stores under data/us/. Resumable.
Earnings events come from Yahoo's visualization API: consensus EPS estimate,
actual, surprise%, minute timestamp, BMO/AMC type.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

sys.path.insert(0, str(bs.EARNINGS_ROOT / ".vendor"))
from curl_cffi import requests as cr

US_DIR = bs.EARNINGS_ROOT / "data" / "us"
EARN_DIR = US_DIR / "earnings"
PX_DIR = US_DIR / "prices"
P1, P2 = 1546300800, 1785000000          # 2019-01-01 .. beyond today


def session_with_crumb():
    s = cr.Session(impersonate="chrome")
    s.get("https://fc.yahoo.com", timeout=30)
    crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb",
                  timeout=30).text
    if not crumb or "Too Many" in crumb:
        raise SystemExit("crumb failed")
    return s, crumb


def constituents(s) -> list[str]:
    dst = US_DIR / "sp500_constituents.json"
    if dst.exists():
        return json.loads(dst.read_text())
    r = s.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
              timeout=60)
    syms = re.findall(r'href="https://www\.nyse\.com/quote/\w+:(\w+)"|'
                      r'href="https://www\.nasdaq\.com/market-activity/stocks/([\w.-]+)"',
                      r.text)
    out = sorted({(a or b).upper().replace(".", "-") for a, b in syms if a or b})
    if len(out) < 400:
        raise SystemExit(f"constituent scrape looks wrong: {len(out)}")
    dst.write_text(json.dumps(out))
    return out


def fetch_earnings(s, crumb, ticker: str) -> bool:
    dst = EARN_DIR / f"{ticker}.json"
    if dst.exists():
        return False
    body = {
        "size": 60, "query": {"operator": "eq", "operands": ["ticker", ticker]},
        "sortField": "startdatetime", "sortType": "DESC",
        "entityIdType": "earnings",
        "includeFields": ["ticker", "startdatetime", "startdatetimetype",
                          "epsestimate", "epsactual", "epssurprisepct"],
    }
    r = s.post("https://query1.finance.yahoo.com/v1/finance/visualization"
               f"?crumb={crumb}", json=body, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"earnings {ticker}: HTTP {r.status_code}")
    docs = r.json()["finance"]["result"][0]["documents"]
    rows = docs[0]["rows"] if docs else []
    dst.write_text(json.dumps(rows))
    return True


def fetch_prices(s, ticker: str) -> bool:
    dst = PX_DIR / f"{ticker}.json"
    if dst.exists():
        return False
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?period1={P1}&period2={P2}&interval=1d&events=div")
    r = s.get(url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"prices {ticker}: HTTP {r.status_code}")
    res = r.json()["chart"]["result"][0]
    if not res.get("timestamp"):
        return False
    dst.write_text(json.dumps(res))
    return True


def main() -> None:
    EARN_DIR.mkdir(parents=True, exist_ok=True)
    PX_DIR.mkdir(parents=True, exist_ok=True)
    s, crumb = session_with_crumb()
    tickers = constituents(s)
    print(f"constituents: {len(tickers)}")
    todo = tickers + ["^GSPC"]
    e_new = p_new = fail = 0
    for i, t in enumerate(todo):
        try:
            if t != "^GSPC":
                if fetch_earnings(s, crumb, t):
                    e_new += 1
                    time.sleep(0.25)
            if fetch_prices(s, t.replace("^", "%5E")
                            if t.startswith("^") else t):
                p_new += 1
                time.sleep(0.25)
        except Exception as ex:
            fail += 1
            print(f"  {t}: {ex}")
            time.sleep(2)
        if (i + 1) % 50 == 0:
            print(f"  progress {i+1}/{len(todo)} (earnings+{e_new} prices+{p_new} fail {fail})")
    n_e = len(list(EARN_DIR.glob("*.json")))
    n_p = len(list(PX_DIR.glob("*.json")))
    print(f"done: earnings files {n_e}, price files {n_p}, failures this run {fail}")


if __name__ == "__main__":
    main()
