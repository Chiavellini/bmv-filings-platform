#!/usr/bin/env python3
"""Build the sendable 63-event Analyst vs Luna post-report trading bundle."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import zipfile
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "earnings/outputs/codex_luna_forced_63/internal_archive_incomplete_ai/handoff_original/trades_63.csv"
OUT = ROOT / "earnings/outputs/codex_luna_forced_63/handoff"
WORKBOOK = ROOT / "outputs/ai_analyst_63_clear_20260824/Analyst_Luna_Post_Report_63.xlsx"
ZIP_PATH = ROOT / "earnings/outputs/codex_luna_forced_63/analyst_luna_63_peer_bundle.zip"


KEEP_COLUMNS = [
    "ticker", "slug", "symbol", "period", "report_date", "report_session",
    "report_timestamp", "timing_source", "report_source_reference", "expected_t0",
    "timing_verified", "t0", "entry_rule", "price_open_t0", "price_close_t0",
    "volume_t0", "index_open_t0", "index_close_t0", "index_ret_open_to_close",
    "raw_ret", "ar0_intra", "ar0_cc", "median_peso_volume", "realized_direction",
    "close_to_close_realized_direction", "label_raw", "analyst_direction",
    "analyst_strength", "analyst_strength_label", "analyst_correct",
    "analyst_correct_close_to_close", "analyst_trade_gross_return",
    "analyst_trade_net_return_25bps", "luna_direction", "luna_analyst_label",
    "luna_strength", "luna_strength_label", "luna_conviction", "luna_reason_code",
    "luna_reason_code_secondary", "luna_guidance_direction", "luna_beat_quality",
    "luna_rationale", "luna_correct", "luna_correct_close_to_close",
    "luna_trade_gross_return", "luna_trade_net_return_25bps",
]


def metric(df: pd.DataFrame, who: str) -> dict:
    return {
        "events": int(len(df)),
        "relative_hits": int(df[f"{who}_correct"].sum()),
        "relative_hit_rate": float(df[f"{who}_correct"].mean()),
        "net_wins": int((df[f"{who}_trade_net_return_25bps"] > 0).sum()),
        "net_win_rate": float((df[f"{who}_trade_net_return_25bps"] > 0).mean()),
        "mean_net_return": float(df[f"{who}_trade_net_return_25bps"].mean()),
    }


def pct(x: float, decimals: int = 1) -> str:
    return f"{x * 100:.{decimals}f}%"


def signed_pct(x: float, decimals: int = 2) -> str:
    return f"{x * 100:+.{decimals}f}%"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    source = pd.read_csv(SOURCE)
    if len(source) != 63:
        raise SystemExit(f"Expected 63 events; found {len(source)}")
    if not source["timing_verified"].astype(bool).all():
        raise SystemExit("Not all reaction-day timings are verified")

    frame = source[KEEP_COLUMNS].copy()
    if any("claude" in column.lower() for column in frame.columns):
        raise SystemExit("Excluded reader column leaked into peer dataset")

    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "Analyst_Luna_63_Events.csv"
    parquet_path = OUT / "Analyst_Luna_63_Events.parquet"
    workbook_path = OUT / WORKBOOK.name
    frame.to_csv(csv_path, index=False)
    frame.to_parquet(parquet_path, index=False)
    shutil.copy2(WORKBOOK, workbook_path)

    analyst = metric(frame, "analyst")
    luna = metric(frame, "luna")
    by_tier = {
        tier: {"events": int(len(group)), "analyst": metric(group, "analyst"), "luna": metric(group, "luna")}
        for tier, group in frame.groupby("analyst_strength_label", sort=False)
    }
    by_confidence = {
        confidence: {"events": int(len(group)), **metric(group, "luna")}
        for confidence, group in frame.groupby("luna_conviction", sort=False)
    }
    paired = {
        "same_direction": int((frame["analyst_direction"] == frame["luna_direction"]).sum()),
        "both_correct": int((frame["analyst_correct"] & frame["luna_correct"]).sum()),
        "analyst_only_correct": int((frame["analyst_correct"] & ~frame["luna_correct"]).sum()),
        "luna_only_correct": int((~frame["analyst_correct"] & frame["luna_correct"]).sum()),
        "both_wrong": int((~frame["analyst_correct"] & ~frame["luna_correct"]).sum()),
    }
    full = frame[frame["analyst_strength_label"].eq("full")]
    conviction = {
        "events": int(len(full)),
        "original_close_to_close_hits": int(full["analyst_correct_close_to_close"].sum()),
        "original_close_to_close_hit_rate": float(full["analyst_correct_close_to_close"].mean()),
        "strict_relative_open_to_close_hits": int(full["analyst_correct"].sum()),
        "strict_relative_open_to_close_hit_rate": float(full["analyst_correct"].mean()),
        "gross_profitable_trades": int((full["analyst_trade_gross_return"] > 0).sum()),
        "gross_profitable_rate": float((full["analyst_trade_gross_return"] > 0).mean()),
        "net_profitable_trades": int((full["analyst_trade_net_return_25bps"] > 0).sum()),
        "net_profitable_rate": float((full["analyst_trade_net_return_25bps"] > 0).mean()),
    }
    summary = {
        "scope": "63 verified post-report reaction sessions; Analyst vs Luna only",
        "timing": {
            "verified": int(frame["timing_verified"].sum()),
            "after_close_next_trading_day": int(frame["report_session"].eq("after_close").sum()),
            "pre_open_same_trading_day": int(frame["report_session"].eq("pre_open").sum()),
        },
        "analyst": analyst,
        "luna": luna,
        "by_analyst_tier": by_tier,
        "luna_by_self_reported_confidence": by_confidence,
        "analyst_full_conviction_definitions": conviction,
        "paired_outcomes": paired,
    }
    (OUT / "SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    with (OUT / "Luna_Responses_63.jsonl").open("w", encoding="utf-8") as handle:
        for row in frame.to_dict(orient="records"):
            payload = {
                "ticker": row["ticker"], "period": row["period"], "reaction_day": row["t0"],
                "realized_direction": row["realized_direction"], "luna_label": row["luna_analyst_label"],
                "luna_direction": row["luna_direction"], "luna_confidence": row["luna_conviction"],
                "luna_rationale": row["luna_rationale"], "relative_hit": bool(row["luna_correct"]),
                "net_return_after_25bps": row["luna_trade_net_return_25bps"],
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    tier_rows = "".join(
        f"<tr><td>{html.escape(str(tier).title())}</td><td>{stats['events']}</td>"
        f"<td>{pct(stats['analyst']['relative_hit_rate'])}</td><td>{pct(stats['luna']['relative_hit_rate'])}</td>"
        f"<td>{pct(stats['analyst']['net_win_rate'])}</td><td>{pct(stats['luna']['net_win_rate'])}</td>"
        f"<td>{signed_pct(stats['analyst']['mean_net_return'])}</td><td>{signed_pct(stats['luna']['mean_net_return'])}</td></tr>"
        for tier, stats in by_tier.items()
    )
    confidence_order = [name for name in ("high", "medium", "low") if name in by_confidence]
    confidence_rows = "".join(
        f"<tr><td>{name.title()}</td><td>{by_confidence[name]['events']}</td>"
        f"<td>{by_confidence[name]['relative_hits']}</td><td>{pct(by_confidence[name]['relative_hit_rate'])}</td>"
        f"<td>{by_confidence[name]['net_wins']}</td><td>{pct(by_confidence[name]['net_win_rate'])}</td>"
        f"<td>{signed_pct(by_confidence[name]['mean_net_return'])}</td></tr>"
        for name in confidence_order
    )
    events = []
    for row in frame.to_dict(orient="records"):
        events.append({
            "ticker": row["ticker"], "period": row["period"], "tier": str(row["analyst_strength_label"]).title(),
            "report_date": row["report_date"], "report_session": str(row["report_session"]).replace("_", " ").title(),
            "reaction_day": row["t0"], "timing_source": row["timing_source"],
            "stock_oc": row["raw_ret"] * 100, "ipc_oc": row["index_ret_open_to_close"] * 100,
            "relative_oc": row["ar0_intra"] * 100, "outcome": str(row["realized_direction"]).title(),
            "analyst_label": row["label_raw"], "analyst_direction": str(row["analyst_direction"]).title(),
            "analyst_hit": bool(row["analyst_correct"]), "analyst_net": row["analyst_trade_net_return_25bps"] * 100,
            "luna_label": row["luna_analyst_label"], "luna_direction": str(row["luna_direction"]).title(),
            "luna_confidence": str(row["luna_conviction"]).title(), "luna_hit": bool(row["luna_correct"]),
            "luna_net": row["luna_trade_net_return_25bps"] * 100, "luna_rationale": row["luna_rationale"],
        })

    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analyst vs Luna · 63 post-report sessions</title>
<style>
:root{{--navy:#16324f;--teal:#0f766e;--paper:#fff;--bg:#eef2f4;--line:#d4dde3;--ink:#17232c;--muted:#60717d;--good:#16734a;--bad:#a83a32}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}}main{{max-width:1240px;margin:auto;padding:28px}}header{{background:var(--navy);color:white;padding:28px;border-radius:14px}}h1{{margin:0 0 7px;font-size:30px}}h2{{margin:0 0 14px}}.sub{{color:#dce7f1}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:18px 0}}.card{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:18px}}.kpi{{font-size:29px;font-weight:750;color:var(--navy)}}.note{{color:var(--muted)}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{background:#e8f0f7;color:var(--navy);position:sticky;top:0}}.scroll{{overflow:auto;max-height:640px;border:1px solid var(--line);border-radius:10px}}.hit{{color:var(--good);font-weight:700}}.miss{{color:var(--bad);font-weight:700}}label{{display:flex;gap:8px;align-items:center}}select{{padding:8px;border:1px solid var(--line);border-radius:8px;background:white}}button{{border:0;border-radius:7px;padding:6px 9px;background:var(--teal);color:white;cursor:pointer}}dialog{{max-width:760px;border:0;border-radius:12px;padding:22px;box-shadow:0 16px 50px #0004}}.downloads a{{display:inline-block;margin:4px 10px 4px 0}}footer{{margin:22px 0;color:var(--muted)}}@media(max-width:760px){{main{{padding:12px}}th,td{{padding:7px}}}}
</style></head><body><main>
<header><h1>Post-report day trading: Analyst vs Luna</h1><div class="sub">63 verified Mexican-equity earnings events · first tradable session after the report · no abstention</div></header>
<section class="grid">
<div class="card"><h2>Timing audit</h2><div class="kpi">63 / 63</div><p>48 after-close reports use the next actual IPC trading day; 15 pre-open reports use the same trading day.</p></div>
<div class="card"><h2>Analyst</h2><div class="kpi">{analyst['relative_hits']}/63 · {pct(analyst['relative_hit_rate'])}</div><p>{analyst['net_wins']}/63 net-profitable after 25 bps · mean {signed_pct(analyst['mean_net_return'])} per trade.</p></div>
<div class="card"><h2>Luna</h2><div class="kpi">{luna['relative_hits']}/63 · {pct(luna['relative_hit_rate'])}</div><p>{luna['net_wins']}/63 net-profitable after 25 bps · mean {signed_pct(luna['mean_net_return'])} per trade.</p></div>
</section>
<section class="card"><h2>Analyst conviction: the 92.6% memory is real, but it is a different return window</h2>
<table><thead><tr><th>Full-conviction events</th><th>Original adjusted C→C hit</th><th>Strict relative O→C hit</th><th>Gross trade win</th><th>Net trade win</th></tr></thead><tbody>
<tr><td>{conviction['events']}</td><td>{conviction['original_close_to_close_hits']}/27 · {pct(conviction['original_close_to_close_hit_rate'])}</td><td>{conviction['strict_relative_open_to_close_hits']}/27 · {pct(conviction['strict_relative_open_to_close_hit_rate'])}</td><td>{conviction['gross_profitable_trades']}/27 · {pct(conviction['gross_profitable_rate'])}</td><td>{conviction['net_profitable_trades']}/27 · {pct(conviction['net_profitable_rate'])}</td></tr>
</tbody></table><p class="note">For literal day trading, use the reaction-session open→close columns. The original 92.6% statistic is retained only to reconcile the earlier analyst research result.</p></section>
<section class="card"><h2>Performance by analyst conviction tier</h2><div class="scroll" style="max-height:none"><table><thead><tr><th>Tier</th><th>Events</th><th>Analyst relative hit</th><th>Luna relative hit</th><th>Analyst net win</th><th>Luna net win</th><th>Analyst mean net</th><th>Luna mean net</th></tr></thead><tbody>{tier_rows}</tbody></table></div></section>
<section class="card"><h2>Luna performance by self-reported confidence</h2><table><thead><tr><th>Confidence</th><th>Events</th><th>Relative hits</th><th>Relative hit rate</th><th>Net wins</th><th>Net win rate</th><th>Mean net</th></tr></thead><tbody>{confidence_rows}</tbody></table><p class="note">High confidence contains only four events, so treat its 100% directional result as descriptive.</p></section>
<section class="card"><h2>Paired outcomes</h2><p>Same direction {paired['same_direction']}/63 · both correct {paired['both_correct']} · analyst only {paired['analyst_only_correct']} · Luna only {paired['luna_only_correct']} · both wrong {paired['both_wrong']}.</p></section>
<section class="card"><h2>All 63 events</h2><label>Analyst tier <select id="tier"><option value="All">All</option><option value="Full">Full</option><option value="Soft">Soft</option></select></label><div class="scroll"><table><thead><tr><th>Issuer</th><th>Quarter</th><th>Reaction day</th><th>Tier</th><th>Outcome</th><th>Analyst</th><th>Luna</th><th>Relative O→C</th><th>Net returns</th><th></th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="card downloads"><h2>Sendable files</h2><a href="{workbook_path.name}">Excel workbook</a><a href="{csv_path.name}">CSV</a><a href="{parquet_path.name}">Parquet</a><a href="Luna_Responses_63.jsonl">Luna responses</a><a href="SUMMARY.json">Summary JSON</a><a href="README.md">README</a><a href="DATA_DICTIONARY.md">Data dictionary</a></section>
<footer>Historical descriptive benchmark, not prospective proof of alpha. Directional correctness uses stock open→close minus IPC open→close on the verified reaction day. Trading return uses raw stock open→close, aligned with the call, less 25 bps.</footer>
<dialog id="detail"><div id="detailBody"></div><form method="dialog"><button>Close</button></form></dialog>
<script>const events={json.dumps(events, ensure_ascii=False)};const rows=document.getElementById('rows'),tier=document.getElementById('tier'),dialog=document.getElementById('detail'),body=document.getElementById('detailBody');const esc=x=>String(x??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));const mark=x=>x?'<span class="hit">✓</span>':'<span class="miss">✕</span>';function render(){{const shown=events.filter(e=>tier.value==='All'||e.tier===tier.value);rows.innerHTML=shown.map((e,i)=>`<tr><td><strong>${{esc(e.ticker)}}</strong></td><td>${{esc(e.period)}}</td><td>${{esc(e.reaction_day)}}</td><td>${{esc(e.tier)}}</td><td>${{esc(e.outcome)}}</td><td>${{esc(e.analyst_label)}} / ${{esc(e.analyst_direction)}} ${{mark(e.analyst_hit)}}</td><td>${{esc(e.luna_label)}} / ${{esc(e.luna_direction)}} ${{mark(e.luna_hit)}}</td><td>${{e.relative_oc>=0?'+':''}}${{e.relative_oc.toFixed(2)}}%</td><td>A ${{e.analyst_net>=0?'+':''}}${{e.analyst_net.toFixed(2)}}% · L ${{e.luna_net>=0?'+':''}}${{e.luna_net.toFixed(2)}}%</td><td><button data-i="${{events.indexOf(e)}}">Details</button></td></tr>`).join('')}}tier.addEventListener('change',render);rows.addEventListener('click',ev=>{{const b=ev.target.closest('button');if(!b)return;const e=events[+b.dataset.i];body.innerHTML=`<h2>${{esc(e.ticker)}} · ${{esc(e.period)}}</h2><p><strong>Timing:</strong> report ${{esc(e.report_date)}} · ${{esc(e.report_session)}}; reaction day ${{esc(e.reaction_day)}}; source ${{esc(e.timing_source)}}.</p><p><strong>Moves:</strong> stock ${{e.stock_oc>=0?'+':''}}${{e.stock_oc.toFixed(2)}}%; IPC ${{e.ipc_oc>=0?'+':''}}${{e.ipc_oc.toFixed(2)}}%; relative ${{e.relative_oc>=0?'+':''}}${{e.relative_oc.toFixed(2)}}%.</p><p><strong>Analyst:</strong> ${{esc(e.analyst_label)}} / ${{esc(e.analyst_direction)}}; net ${{e.analyst_net>=0?'+':''}}${{e.analyst_net.toFixed(2)}}%.</p><p><strong>Luna:</strong> ${{esc(e.luna_label)}} / ${{esc(e.luna_direction)}} / ${{esc(e.luna_confidence)}}; net ${{e.luna_net>=0?'+':''}}${{e.luna_net.toFixed(2)}}%.</p><p>${{esc(e.luna_rationale)}}</p>`;dialog.showModal()}});render();</script>
</main></body></html>"""
    html_path = OUT / "OPEN_ME_FIRST.html"
    html_path.write_text(document, encoding="utf-8")

    readme = f"""# Analyst vs Luna: 63 post-report reaction sessions

Open `OPEN_ME_FIRST.html` first. This is the peer-facing bundle.

- All 63 report timings are verified.
- After-close reports use the next actual IPC trading day; pre-open reports use the same trading day.
- Directional correctness uses stock open-to-close minus IPC open-to-close.
- Executable trade return uses raw stock open-to-close aligned with the call, less 25 bps.
- Luna was forced to choose from the same labels available to the analyst; abstention was not allowed.
- The workbook contains Summary, Event Results, Luna Responses, and Methodology sheets.
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    dictionary = """# Data dictionary

The stable event key is `slug + period`.

| Field | Meaning |
|---|---|
| `report_session` | `after_close` or `pre_open`. |
| `t0` | Verified reaction session used for trading. |
| `raw_ret` | Stock open-to-close return on `t0`. |
| `index_ret_open_to_close` | IPC open-to-close return on `t0`. |
| `ar0_intra` | Stock return minus IPC return on `t0`; used for directional correctness. |
| `ar0_cc` | Adjusted close-to-close research outcome, retained for reconciliation only. |
| `*_correct` | Direction matches the sign of `ar0_intra`. |
| `*_trade_net_return_25bps` | Direction-aligned `raw_ret`, less 25 bps. |
| `analyst_strength_label` | Original analyst call tier: full or soft. |
| `luna_conviction` | Luna's self-reported confidence: high, medium, or low. |
| `luna_rationale` | Actual saved rationale for the forced-choice Luna call. |
"""
    (OUT / "DATA_DICTIONARY.md").write_text(dictionary, encoding="utf-8")
    (OUT / "START_HERE.txt").write_text("Open OPEN_ME_FIRST.html. The Excel workbook is the best handoff to a dashboard developer.\n", encoding="utf-8")

    files = [path for path in OUT.iterdir() if path.is_file() and path.name != "BUNDLE_MANIFEST.json"]
    manifest = {"primary_entrypoint": "OPEN_ME_FIRST.html", "scope": "Analyst vs Luna; 63 verified events", "files": {path.name: sha256(path) for path in sorted(files)}}
    (OUT / "BUNDLE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(OUT.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)

    if "claude" in document.lower() or any("claude" in path.name.lower() for path in OUT.iterdir()):
        raise SystemExit("Excluded reader leaked into peer-facing bundle")
    print(json.dumps({"output": str(OUT), "zip": str(ZIP_PATH), "events": len(frame), "files": len(list(OUT.iterdir()))}))


if __name__ == "__main__":
    main()
