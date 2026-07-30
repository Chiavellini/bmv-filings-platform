#!/usr/bin/env python3
"""build_prices.py — frozen daily OHLCV panel + ^MXX index series.

Modes:
  --snapshot-only   copy soft/.cache/market_data/yf_*.json into the immutable
                    snapshot dir and write a SHA manifest (idempotent; never
                    overwrites an existing snapshot file).
  --fetch-index     the study's ONE network call: fetch ^MXX daily bars with a
                    minimal replica of soft's chart client, saved into the
                    snapshot dir (soft's cache is never touched).
  (default)         convert every snapshot file to outputs/prices.parquet and
                    outputs/index_mxx.parquet.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

INDEX_SYMBOL = "^MXX"
INDEX_FILE = "yf_MXX_INDEX.json"
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "%5EMXX?range=6y&interval=1d&events=div")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def snapshot() -> None:
    src = bs.SOFT_ROOT / ".cache" / "market_data"
    bs.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for f in sorted(src.glob("yf_*.json")):
        dst = bs.SNAPSHOT_DIR / f.name
        if dst.exists():
            skipped += 1
            continue
        shutil.copy2(f, dst)
        copied += 1
    manifest = {
        "snapshot_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "repo_head": bs.repo_head(),
        "files": {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(bs.SNAPSHOT_DIR.glob("yf_*.json"))
        },
    }
    (bs.SNAPSHOT_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    print(f"snapshot: copied {copied}, kept existing {skipped}, "
          f"total {len(manifest['files'])} files; manifest written")


MAX_DIR_NAME = "market_data_max"


def _http_get(url: str, timeout: int = 45):
    """Chrome-TLS-impersonated GET (Yahoo edge blocks plain requests/curl by
    TLS fingerprint — diagnosed 2026-07-27); falls back to requests."""
    vendor = str(bs.EARNINGS_ROOT / ".vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        from curl_cffi import requests as cr
        return cr.get(url, impersonate="chrome", timeout=timeout)
    except ImportError:
        import requests
        return requests.get(url, headers={"User-Agent": UA}, timeout=timeout)


def _get_chart(symbol_url: str, timeout: int = 45):
    # period1/period2 (not range=max): range=max silently degrades to monthly
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{symbol_url}?period1=631152000&period2=1785000000"
           "&interval=1d&events=div")
    resp = None
    for attempt in range(4):
        time.sleep(0.4)
        resp = _http_get(url, timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
            time.sleep(max(2.0 ** attempt, retry_after))
            continue
        break
    resp.raise_for_status()
    return resp.json()["chart"]["result"][0]


def fetch_max() -> None:
    """Fetch range=max daily bars for every universe symbol + ^MXX into the
    separate immutable max-store. Resumable: existing files are skipped, so a
    429 mid-sweep just means re-running later continues where it stopped."""
    from earnlib.bootstrap import load_universe

    max_dir = bs.SNAPSHOT_DIR.parent / MAX_DIR_NAME
    max_dir.mkdir(parents=True, exist_ok=True)
    cfg = bs.load_config()
    targets = [("%5EMXX", INDEX_FILE)]
    for v in load_universe(cfg, "full").values():
        sym = v["symbol"].replace(".MX", "")
        targets.append((sym.replace("&", "%26") + ".MX", f"yf_{sym}_MX.json"))

    done = fetched = 0
    for url_sym, fname in targets:
        dst = max_dir / fname
        if dst.exists():
            done += 1
            continue
        result = _get_chart(url_sym)      # raises on persistent 429 -> rerun resumes
        n = len(result.get("timestamp") or [])
        if n == 0:
            print(f"  {fname}: EMPTY result, skipping write")
            continue
        dst.write_text(json.dumps(result))
        first = datetime.fromtimestamp(result["timestamp"][0]).date()
        print(f"  {fname}: {n} bars since {first}")
        fetched += 1
    manifest = {
        "snapshot_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "files": {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in sorted(max_dir.glob("yf_*.json"))},
    }
    (max_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    print(f"max-store: fetched {fetched} new, {done} already present, "
          f"total {len(manifest['files'])}/{len(targets)}")
    missing = len(targets) - len(manifest["files"])
    if missing:
        print(f"WARN {missing} symbols with no max history (delisted/empty) — "
              "the 6y snapshot covers them via fallback")


def fetch_index() -> None:
    dst = bs.SNAPSHOT_DIR / INDEX_FILE
    if dst.exists():
        print(f"{dst} already exists — refusing to overwrite the frozen index series")
        return
    resp = None
    for attempt in range(5):
        time.sleep(0.4 + attempt * 4)     # polite throttle + linear backoff
        resp = _http_get(CHART_URL, 30)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = float(resp.headers.get("Retry-After", 0) or 0)
            wait = max(2.0 ** attempt, retry_after)
            print(f"HTTP {resp.status_code}, retrying in {wait:.0f}s")
            time.sleep(wait)
            continue
        break
    resp.raise_for_status()
    payload = resp.json()
    result = payload["chart"]["result"][0]     # same shape soft caches
    dst.write_text(json.dumps(result))
    print(f"fetched ^MXX: {len(result.get('timestamp', []))} bars -> {dst}")


def bars_frame(result: dict, symbol: str) -> pd.DataFrame:
    ts = result.get("timestamp") or []
    if not ts:
        return pd.DataFrame()
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    tz = ZoneInfo(result.get("meta", {}).get("exchangeTimezoneName", "America/Mexico_City"))
    rows = {
        "symbol": symbol,
        "date": [datetime.fromtimestamp(t, tz).date() for t in ts],
        "open": quote.get("open"), "high": quote.get("high"),
        "low": quote.get("low"), "close": quote.get("close"),
        "volume": quote.get("volume"),
        "adjclose": adj if adj is not None else quote.get("close"),
    }
    df = pd.DataFrame(rows)
    df = df[df["close"].notna()].copy()
    df["adj_factor"] = df["adjclose"] / df["close"]
    return df


def ew_proxy(prices: pd.DataFrame) -> pd.DataFrame:
    """Equal-weight market proxy from all snapshot symbols (fallback while
    Yahoo throttles ^MXX; logged as a researcher degree of freedom).

    Daily return = mean adjclose return across symbols with a bar both days;
    open proxied by the previous close compounded with the mean open/prev-close
    gap so overnight/intraday decompositions stay defined.
    """
    wide_adj = prices.pivot_table(index="date", columns="symbol", values="adjclose")
    wide_open = prices.pivot_table(index="date", columns="symbol", values="open")
    wide_close = prices.pivot_table(index="date", columns="symbol", values="close")
    wide_fac = prices.pivot_table(index="date", columns="symbol", values="adj_factor")

    ret = wide_adj / wide_adj.shift(1) - 1
    gap = (wide_open * wide_fac) / (wide_close * wide_fac).shift(1) - 1
    mean_ret = ret.mean(axis=1)
    mean_gap = gap.mean(axis=1)

    level = 100.0 * (1.0 + mean_ret.fillna(0)).cumprod()
    open_level = level.shift(1) * (1.0 + mean_gap.fillna(0))
    df = pd.DataFrame({
        "symbol": "EWPROXY", "date": level.index,
        "open": open_level.values, "high": level.values, "low": level.values,
        "close": level.values, "adjclose": level.values,
        "volume": 0.0, "adj_factor": 1.0,
    })
    return df.dropna(subset=["open"]).reset_index(drop=True)


def convert() -> None:
    max_dir = bs.SNAPSHOT_DIR.parent / MAX_DIR_NAME
    frames = []
    index_df = None
    used_max = checked = 0
    for f in sorted(bs.SNAPSHOT_DIR.glob("yf_*.json")):
        result = json.loads(f.read_text())
        maxf = max_dir / f.name
        max_result = json.loads(maxf.read_text()) if maxf.exists() else None
        if f.name == INDEX_FILE:
            index_df = bars_frame(max_result or result, INDEX_SYMBOL)
            continue
        symbol = f.name[len("yf_"):-len("_MX.json")] + ".MX"
        snap_df = bars_frame(result, symbol)
        if max_result is not None:
            max_df = bars_frame(max_result, symbol)
            # overlap sanity: raw closes must agree on common dates
            merged = snap_df.merge(max_df, on="date", suffixes=("_s", "_m"))
            if len(merged) >= 50:
                rel = ((merged["close_s"] - merged["close_m"]).abs()
                       / merged["close_m"].clip(lower=1e-9))
                assert rel.median() < 0.001, f"{symbol}: max-store disagrees with snapshot"
                checked += 1
            frames.append(max_df)
            used_max += 1
        else:
            frames.append(snap_df)
    # max-store may include the index even if the 6y snapshot never got it
    if index_df is None and (max_dir / INDEX_FILE).exists():
        index_df = bars_frame(json.loads((max_dir / INDEX_FILE).read_text()),
                              INDEX_SYMBOL)
    if used_max:
        print(f"max-store used for {used_max} symbols "
              f"(overlap-checked: {checked}); 6y snapshot for the rest")
    raw = bs.SNAPSHOT_DIR / "mxx_raw.json"        # manual-download escape hatch
    if index_df is None and raw.exists():
        payload = json.loads(raw.read_text())
        result = payload["chart"]["result"][0] if "chart" in payload else payload
        (bs.SNAPSHOT_DIR / INDEX_FILE).write_text(json.dumps(result))
        index_df = bars_frame(result, INDEX_SYMBOL)
        print("adopted manually downloaded mxx_raw.json as the frozen ^MXX series")

    prices = pd.concat([fr for fr in frames if len(fr)], ignore_index=True)
    if index_df is None:
        print("NO ^MXX snapshot — falling back to the equal-weight proxy "
              "(logged as researcher degree of freedom; re-run --fetch-index "
              "to upgrade to the real index)")
        index_df = ew_proxy(prices)
    bs.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    # audit_v2 issue D: drop corrupt adjusted closes and near-empty symbols,
    # loudly — the dropped log names every casualty (e.g. TLEVISAB's 1 bar).
    cfg = bs.load_config()
    if "audit_v2" in cfg:
        from earnlib.quality import filter_price_quality
        prices, dropped = filter_price_quality(
            prices, cfg["audit_v2"]["price_quality"]["min_bars_per_symbol"])
        bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        dropped.to_csv(bs.RESULTS_DIR / "price_quality_dropped.csv", index=False)
        if len(dropped):
            print(f"price quality: dropped {len(dropped)} symbol-issues:")
            print(dropped.to_string(index=False))

    prices.to_parquet(bs.art_path("prices"), index=False)
    index_df.to_parquet(bs.OUTPUTS_DIR / "index_mxx.parquet", index=False)

    # ---- verify block ----
    cfg = bs.load_config()
    uni_syms = {v["symbol"] for v in cfg["universe"].values()}
    n_mxx = len(index_df)
    print(f"prices.parquet: {len(prices)} rows, {prices['symbol'].nunique()} symbols")
    print(f"^MXX: {n_mxx} bars, {index_df['date'].min()} .. {index_df['date'].max()}")
    print("\nuniverse symbols vs ^MXX calendar:")
    mxx_dates = set(index_df["date"])
    for sym in sorted(uni_syms):
        sub = prices[prices["symbol"] == sym]
        overlap = sub[sub["date"].isin(mxx_dates)]
        missing = 1 - len(overlap) / n_mxx
        zerovol = (sub["volume"].fillna(0) == 0).mean()
        flag = "  <-- THIN" if (missing > 0.05 or zerovol > 0.10) else ""
        print(f"  {sym:16s} bars={len(sub):5d} missing_vs_mxx={missing:5.1%} "
              f"zero_vol={zerovol:5.1%}{flag}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-only", action="store_true")
    ap.add_argument("--fetch-index", action="store_true")
    ap.add_argument("--fetch-max", action="store_true",
                    help="range=max history for all universe symbols + ^MXX (resumable)")
    args = ap.parse_args()
    if args.snapshot_only:
        snapshot()
    elif args.fetch_max:
        fetch_max()
        convert()
    elif args.fetch_index:
        fetch_index()
        convert()
    else:
        convert()
