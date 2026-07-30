"""Coverage validation gate — sanity-check a built :class:`CoverageModel` and the underlying
filings fundamentals, and render a markdown report.

Two layers:
  1. Fundamentals — reuse the vendored ``src.shared.validator`` accounting identities on the
     subject's LTM values (what the releases support).
  2. Coverage — engine-specific sanity checks (EV bridge ties out, multiples in plausible
     ranges, peer set populated, historical band non-degenerate, blank cells surfaced).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Plausible ranges for headline multiples (equity/retail); outside → flagged, not fatal.
_MULTIPLE_RANGES = {
    "P/E (LTM)": (2, 80),
    "P/E (fwd)": (2, 80),
    "EV/EBITDA": (1, 40),
    "EV/Sales": (0.1, 15),
    "P/BV": (0.2, 25),
    "P/FCF": (2, 120),
}

# Cells a complete deliverable MUST carry, per template. Each entry is
# ``(label, kind, needs_price)``:
#   kind="engine" → derivable from XBRL (+ a market price); a blank here is an ENGINE GAP (a bug we
#      own) and must fail loudly, NOT be demoted to a worklist note.
#   kind="data"   → sourced from Bloomberg / consensus / macro feeds the user supplies; a blank is
#      PENDING external data, listed honestly but not a code defect.
#   needs_price=True → the cell also needs a market price; when no price was fetched (offline build)
#      a blank is reclassified as pending market-data, so offline runs don't false-alarm as broken.
_REQUIRED = {
    "financials": [
        ("Book value", "engine", False),
        ("ROE", "engine", False),
        ("ROTE", "engine", False),
        ("P/E (LTM)", "engine", True),
        ("P/BV", "engine", True),
        ("P/TBV", "engine", True),
        ("Dividend yield", "data", False),
        ("Net interest margin (NIM)", "data", False),
        ("CET1 ratio", "data", False),
        # P/E (fwd) [forward consensus EPS] and loan/deposit growth are Bloomberg-only (forward /
        # untagged in XBRL) → advisory, NOT required: a bank real-complete on everything else is a
        # ✅ PASS, and these show as a clearly-marked optional top-up rather than blocking completion.
    ],
    "industrial": [
        ("P/E (LTM)", "engine", True),
        ("P/BV", "engine", True),
        ("EV/EBITDA", "data", True),   # needs net debt (Bloomberg/derived) → data, not engine
        ("Dividend yield", "data", False),
        # P/E (fwd) [forward consensus EPS] is Bloomberg-only → advisory, not required.
    ],
    "reit": [
        ("P/FFO", "data", True),
        ("P/NAV", "data", True),
        ("Distribution yield", "data", False),
    ],
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class CoverageValidation:
    checks: list[Check] = field(default_factory=list)
    blank_cells: list[str] = field(default_factory=list)
    missing_engine: list[str] = field(default_factory=list)  # required cells blank due to an engine gap
    missing_data: list[str] = field(default_factory=list)    # required cells pending external data
    sentinels: list[str] = field(default_factory=list)       # present-but-wrong values (always bugs)
    notes: list[str] = field(default_factory=list)           # expected "not meaningful" values (non-blocking)

    @property
    def checks_passed(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def status(self) -> str:
        """PASS · INCOMPLETE (external data pending) · BROKEN (engine gap or wrong value)."""
        if self.missing_engine or self.sentinels:
            return "BROKEN"
        if self.missing_data:
            return "INCOMPLETE"
        return "PASS"

    @property
    def passed(self) -> bool:
        # A deliverable is only truly "passed" when complete AND every sanity check holds.
        return self.status == "PASS" and self.checks_passed


def validate_model(model, spec, fund, pack) -> CoverageValidation:
    v = CoverageValidation()
    by_id = {b.id: b for b in model.blocks}

    # 1) Snapshot multiples within plausible ranges.
    snap = by_id.get("snapshot_multiples")
    if snap:
        for cell in snap.rows:
            rng = _MULTIPLE_RANGES.get(cell.label)
            # A non-positive multiple is "not meaningful" (negative denominator — loss-maker), reported
            # as a note below; it is not an out-of-plausible-range value, so it is not range-checked.
            if rng and cell.value is not None and cell.value > 0:
                lo, hi = rng
                ok = lo <= cell.value <= hi
                v.checks.append(Check(f"{cell.label} in [{lo},{hi}]", ok,
                                      f"{cell.value:.1f}x" if not ok else f"{cell.value:.1f}x"))

    # 2) Peer set populated (each peer has at least price + one fundamental).
    for p in spec.peers:
        pd = pack.peers.get(p.slug, {})
        ok = "px_last" in pd and any(k in pd for k in ("ebitda_ltm", "sales_ltm", "net_income_ltm"))
        v.checks.append(Check(f"peer {p.slug} populated", ok,
                              "ok" if ok else "missing price or fundamentals"))

    # 3) EV bridge ties out (EV = mktcap + net debt + minority) within rounding.
    px = pack.subject.get("px_last")
    sh = pack.subject.get("shares_out")
    nd = pack.subject.get("net_debt")
    if px is not None and sh is not None and nd is not None:
        ev = px * sh + nd + (pack.subject.get("minority_interest") or 0.0)
        v.checks.append(Check("EV bridge computable", ev > 0, f"EV={ev:,.0f}"))

    # 4) Historical band non-degenerate (σ > 0 over ≥3 points).
    hist = by_id.get("historical_multiples")
    if hist:
        pts = [c.value for c in hist.rows if c.label.startswith("FY") and c.value is not None]
        ok = len(pts) >= 3 and (max(pts) - min(pts)) > 1e-6
        v.checks.append(Check("historical band non-degenerate", ok,
                              f"{len(pts)} FY points"))

    # 5) Surface blank cells across all blocks (for the manual worklist).
    for b in model.blocks:
        for c in b.rows:
            if c.value is None:
                v.blank_cells.append(f"{b.id}: {c.label}")

    # 6) Completeness gate — required cells must be present. A blank is an ENGINE GAP (bug) unless
    #    it is external data the user supplies, or a price-dependent cell on a price-less build.
    label_val: dict[str, object] = {}
    for b in model.blocks:
        for c in b.rows:
            label_val.setdefault(c.label, c.value)  # first occurrence of each label
    has_price = pack.subject.get("px_last") is not None
    # A subject with no P&L at all (neither revenue nor net income extracted) is UN-SOURCED — its
    # filings aren't cached or are too thin to extract (a real operating company always reports both).
    # Its blank required cells are then a CORPUS gap (pending data), not an engine bug: an engine fix
    # can't derive a ratio from filings that aren't there. Reclassify so a name awaiting its corpus
    # reads as INCOMPLETE, not BROKEN. (Revenue present but net income missing is a genuine extraction
    # gap, NOT un-sourced — it stays an engine flag.)
    subject_unsourced = fund.get("revenue") is None and fund.get("net_income") is None
    for label, kind, needs_price in _REQUIRED.get(getattr(spec, "template", ""), []):
        if label not in label_val or label_val[label] is not None:
            continue  # not part of this spec, or present → fine
        if kind == "engine" and (has_price or not needs_price) and not subject_unsourced:
            v.missing_engine.append(label)
        else:
            v.missing_data.append(label)

    # 6b) Placeholder Bloomberg pack — dummy round-number data masquerading as real. Any cell it
    #     filled is fabricated, so the deliverable must not read as complete: flag it as a hard
    #     failure (→ BROKEN) naming the tells, so it can't ship as a green PASS.
    if getattr(pack, "suspect_placeholder", False):
        why = "; ".join(getattr(pack, "placeholder_reasons", []) or ["dummy round-number values"])
        v.sentinels.append(f"Bloomberg pack looks like PLACEHOLDER data ({why}) — replace with real "
                           f"terminal values before shipping")

    # 6c) Price/share sanity via P/S — a non-financial's market cap should be a sane multiple of its
    #     revenue. A P/S far outside the band means the live price resolved to the wrong instrument
    #     (Vasconia's Yahoo quote → a penny look-alike, market cap 31mn on 2.4bn revenue) or the share
    #     count is mis-scaled — either way every price-driven multiple (P/E, P/BV, EV/x) is wrong, so
    #     flag it as a hard defect rather than let a silently-wrong market cap ship. (Banks excluded:
    #     revenue isn't the right base for a financial, and they have their own cheap/rich guards.)
    if getattr(spec, "template", "") != "financials":
        mc = pack.subject.get("px_last")
        sh = pack.subject.get("shares_out")
        rev = fund.get("revenue")
        if mc is not None and sh is not None and rev not in (None, 0):
            ps = (mc * sh) / rev
            if not (_PS_MIN <= ps <= _PS_MAX):
                v.sentinels.append(
                    f"snapshot_multiples: P/Sales = {ps:.2f} out of [{_PS_MIN:g}, {_PS_MAX:g}] "
                    f"(market cap {mc * sh:,.0f} / revenue {rev:,.0f}) — the live price or share count "
                    f"is wrong, so every price-driven multiple is unreliable")

    # 6d) Internal consistency: P/E ≈ P/BV / (ROE/100). Both sides equal market-cap÷book — one via
    #     price×shares, one via net income — so the identity holds for any real company. A wide gap means
    #     one of the three is off: a mis-scaled numerator, a wrong-period net income, or a units/CPO
    #     share error (the tell that would have flagged FEMSA's 5× market cap). Advisory NOTE, not a
    #     hard defect — a one-off-earnings quarter opens a genuine gap; REITs are excluded (fair-value
    #     gains break the identity by design).
    if getattr(spec, "template", "") != "reit":
        def _cellv(bid, label):
            b = by_id.get(bid)
            return next((c.value for c in b.rows if c.label == label and c.value is not None), None) if b else None
        pe = _cellv("snapshot_multiples", "P/E (LTM)")
        pbv = _cellv("financial_analysis", "P/BV")
        roe = _cellv("financial_analysis", "ROE")
        if pe and pbv and roe and roe > 0:
            implied = pbv / (roe / 100.0)
            if abs(pe - implied) / pe > 0.5:
                v.notes.append(
                    f"consistency: P/E {pe:.1f} vs P/BV/(ROE) {implied:.1f} disagree — one of "
                    f"P/E / P/BV {pbv:.2f} / ROE {roe:.1f}% is off (period mismatch or share/units error)")

    # 7) Sentinels — present values that are self-evidently wrong (extraction/derivation artifacts).
    is_bank = getattr(spec, "template", "") == "financials"
    for b in model.blocks:
        for c in b.rows:
            if c.value is None:
                continue
            lab = c.label.lower()
            if lab == "book value" and c.value == 0:
                v.sentinels.append(f"{b.id}: Book value = 0 (equity extraction artifact)")
            elif "growth" in lab and c.value == -100.0:
                v.sentinels.append(f"{b.id}: {c.label} = −100% (zero-base artifact)")
            # A non-positive CURRENT multiple is not a computation bug: with a positive price and share
            # count, a multiple can only go negative when its DENOMINATOR is negative — i.e. the company
            # is loss-making (P/E), has negative book (P/BV) or negative FCF/EBITDA. That is a real
            # business state, "not meaningful", and must not mark an otherwise-complete deliverable
            # BROKEN — it is reported as a non-blocking note (whether the negative denominator is itself
            # a filing/extraction error is a separate concern, caught by the accounting-identity gate).
            # Historical statistical BAND endpoints (mean / ±1σ / percentile) can also go negative when
            # σ is wide — likewise a note, not a defect.
            elif (c.unit == "x" and c.value <= 0 and "historical" not in b.id
                  and not any(t in lab for t in ("σ", "sigma", "mean", "percentile", "band"))):
                driver = _nonpositive_driver(c.label, fund)
                v.notes.append(f"{b.id}: {c.label} = {c.value:.2f}x — not meaningful{driver}")
            # A return ratio (ROE/ROA/ROTE/ROIC) above 100% is arithmetically impossible for a
            # going concern — it means the denominator (equity/assets/invested capital) was extracted
            # at the wrong scale (e.g. WALMEX ROA came out 10,078% because total assets landed ~500×
            # too small). Flag so a mis-scaled balance-sheet item can't ship as a headline return.
            elif c.unit == "pct" and c.label.split()[0].upper() in _RETURN_LABELS and c.value > 100:
                v.sentinels.append(f"{b.id}: {c.label} = {c.value:,.0f}% impossible — denominator "
                                   f"(assets/equity) mis-scaled")
            # A bank SUBJECT multiple far outside its plausible band is almost always a bad live
            # price or a mis-derived share count (net-income÷EPS mis-scaled), NOT a real valuation —
            # e.g. Regional's ~10× price → P/BV 9.5×, GBM's ~4× share count → P/E 146×. Flag so the
            # headline can't ship silently wrong. (Peer cells are already blanked to n/m upstream.)
            elif is_bank and c.unit == "x" and c.value > _BANK_SUBJECT_MAX.get(c.label, float("inf")):
                # Same corroboration as the cheap case: the flag's premise is a mis-derived price /
                # share count. When Yahoo's independent share count matches ours (or its own multiple
                # is comparably rich), the multiple is a real low-earnings / richly-valued bank, not an
                # artifact — downgrade to a note. Uncorroborated → stays a flagged bug.
                if _yahoo_corroborates(c.label, c.value, pack):
                    v.notes.append(f"{b.id}: {c.label} = {c.value:.1f}x — above the typical bank range "
                                   f"but the share count is corroborated by Yahoo (a real low-earnings / "
                                   f"rich multiple, not a price/share-count artifact)")
                else:
                    v.sentinels.append(f"{b.id}: {c.label} = {c.value:.1f}x implausible for a bank — "
                                       f"verify live price / share count")
            # Implausibly CHEAP subject multiples (P/E below ~6, P/BV below ~0.6) usually mean the
            # derived share count is too LOW (net-income÷EPS understated it → market cap too small),
            # exactly the AC case. BUT many small/mid-cap BMV names genuinely trade below book — so
            # before flagging, corroborate against Yahoo's own computed multiple: when the independent
            # figure lands in the same neighbourhood, the cheapness is real (a discount, not an
            # artifact) and it is downgraded to a non-blocking note; only an uncorroborated cheap
            # multiple (no second source, or one that disagrees) is a share-count bug.
            elif c.label in _SUBJECT_MIN and c.value is not None and 0 < c.value < _SUBJECT_MIN[c.label]:
                # The sentinel's premise is a too-LOW share count. Yahoo corroboration disproves it
                # directly; absent Yahoo (it doesn't cover small/illiquid BMV names), fall back to the
                # premise itself — a share count of plausible listed-company magnitude means the
                # cheapness is a real discount, not a deflated market cap. Only an implausible/absent
                # share count keeps the hard flag. This stops permanently BROKEN-flagging genuinely
                # cheap small financials (FINDEP ~0.4x book, GF Multiva ~0.5x) that Yahoo can't confirm.
                if _yahoo_corroborates(c.label, c.value, pack):
                    v.notes.append(f"{b.id}: {c.label} = {c.value:.2f}x — below the "
                                   f"{_SUBJECT_MIN[c.label]:g}x floor but corroborated by Yahoo (real "
                                   f"discount to book/earnings, not a share-count artifact)")
                elif _share_count_plausible(pack):
                    v.notes.append(f"{b.id}: {c.label} = {c.value:.2f}x — below the "
                                   f"{_SUBJECT_MIN[c.label]:g}x floor but the share count is of normal "
                                   f"listed-company magnitude (a real discount, not a share-count artifact; "
                                   f"unconfirmed by Yahoo)")
                else:
                    v.sentinels.append(f"{b.id}: {c.label} = {c.value:.2f}x implausibly cheap — verify "
                                       f"share count (a too-low count deflates every market multiple)")

    return v


# Plausible market-cap-to-revenue band for a non-financial. Outside it, the price or share count is
# almost certainly wrong (a resolved-to-wrong-instrument price, or a mis-scaled count). Matches the
# independent reconciler's P/S guard so the build status and the reconciliation agree.
_PS_MIN, _PS_MAX = 0.05, 40.0

# Upper bound on a plausible bank SUBJECT multiple; above this is treated as a data artifact.
_BANK_SUBJECT_MAX = {"P/E (LTM)": 40.0, "P/E (fwd)": 60.0, "P/BV": 5.0, "P/TBV": 5.0}

# Return ratios that are bounded well under 100% for any real company — a value over 100% signals a
# mis-scaled denominator. Matched on the label's first word (e.g. "ROA", "ROE", "ROIC").
_RETURN_LABELS = {"ROE", "ROA", "ROTE", "ROIC"}

# Floors below which a subject multiple is almost certainly a too-low share count (deflated market
# cap), not a genuine deep-value name. Kept conservative so real cheap stocks aren't false-flagged.
_SUBJECT_MIN = {"P/E (LTM)": 6.0, "P/BV": 0.6, "EV/EBITDA": 3.0}

# The filing figure that drives each multiple negative — used to name the reason a multiple is n/m.
_MULTIPLE_DRIVER = {
    "P/E": ("net_income", "net loss"),
    "EV/EBITDA": ("ebitda", "negative EBITDA"),
    "P/FCF": ("fcf", "negative FCF"),
    "P/BV": ("total_equity", "negative book value"),
    "P/TBV": ("tangible_book", "negative tangible book"),
}


# The Yahoo key-stats figure that corresponds to each engine multiple — used to corroborate a
# suspiciously-cheap value against an independent second source before treating it as a bug.
_YAHOO_MULTIPLE_KEY = {"P/BV": "price_to_book", "P/E (LTM)": "trailing_pe", "EV/EBITDA": "ev_ebitda"}


# A listed company's share count sits in a broad but bounded magnitude window. Below the floor the
# count is almost certainly a mis-derived (net-income÷EPS) artifact that deflates the market cap;
# within the window it is a normal count, so a cheap multiple is a real discount, not an artifact.
_MIN_LISTED_SHARES_MN = 20.0        # 20mn shares — below this, a listed-equity count is suspect
_MAX_LISTED_SHARES_MN = 200_000.0   # 200bn shares — above this, a scale/units artifact


def _share_count_plausible(pack) -> bool:
    """True when the subject's share count is of normal listed-company magnitude — used to decide
    whether an uncorroborated cheap multiple is a real discount (plausible count) or a share-count
    artifact (implausible/absent count)."""
    sh = (getattr(pack, "subject", None) or {}).get("shares_out")
    return isinstance(sh, (int, float)) and _MIN_LISTED_SHARES_MN <= sh <= _MAX_LISTED_SHARES_MN


def _yahoo_corroborates(label: str, value: float, pack) -> bool:
    """True when Yahoo's independent data confirms a below-floor multiple is a genuine market discount
    rather than a share-count artifact. Two ways, either sufficient:

      1. Share-count match — the sentinel's whole premise is that a too-LOW share count deflates *every*
         market multiple; when Yahoo's own shares-outstanding agrees with ours (±15%) that premise is
         disproven, so all of this name's cheap multiples are real at once (and the price is already
         Yahoo's, so the market cap ties out too).
      2. Same-metric match — Yahoo's own computed P/BV / P/E / EV-EBITDA lands within ±35% of ours.

    False when Yahoo offers neither (offline build / untracked name), so an uncorroborated cheap value
    stays a flagged bug (conservative)."""
    ys = getattr(pack, "yahoo_stats", None) or {}
    y_sh = ys.get("shares_out")
    o_sh = (getattr(pack, "subject", None) or {}).get("shares_out")
    if (isinstance(y_sh, (int, float)) and isinstance(o_sh, (int, float)) and y_sh > 0 and o_sh > 0
            and 0.85 <= (o_sh / y_sh) <= 1.15):
        return True
    yk = _YAHOO_MULTIPLE_KEY.get(label)
    yv = ys.get(yk) if yk else None
    if not isinstance(yv, (int, float)) or yv <= 0 or value is None or value <= 0:
        return False
    return 0.65 <= (yv / value) <= 1.35


def _nonpositive_driver(label: str, fund) -> str:
    """A short ` (driver: value)` suffix naming why a multiple is non-positive, from the filing figure
    behind it (loss-making net income, negative EBITDA/FCF/equity). Empty when it can't be resolved."""
    for prefix, (key, phrase) in _MULTIPLE_DRIVER.items():
        if label.startswith(prefix):
            try:
                val = fund.get(key)
            except Exception:
                val = None
            if isinstance(val, (int, float)) and val < 0:
                return f" ({phrase} {val:,.0f} in filings)"
            return f" ({phrase})"
    return ""


def _fundamentals_identities(fund):
    """Run the vendored accounting-identity validator on the subject LTM values."""
    try:
        from src.extract.extract_metrics import MetricRow
        from src.shared.validator import validate
    except Exception:
        return []
    metrics = {}
    for k, val in fund.ltm.items():
        metrics[k] = MetricRow(metric=k, label_es=k, current=val, prior=None,
                               var_pct=None, unit="currency", source_line="[filing]")
    try:
        return validate(metrics)
    except Exception:
        return []


def write_report(model, spec, fund, pack, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    v = validate_model(model, spec, fund, pack)
    ident = _fundamentals_identities(fund)

    lines: list[str] = []
    lines.append(f"# {model.name} — Soft Coverage validation")
    lines.append("")
    lines.append(f"- As of: **{model.current_period}**  ·  {model.currency} {model.units}")
    lines.append(f"- Blocks: {', '.join(b.id for b in model.blocks)}")
    status = v.status
    if status == "BROKEN":
        n = len(v.missing_engine) + len(v.sentinels)
        tail = f"; {len(v.missing_data)} also need Bloomberg data" if v.missing_data else ""
        verdict = f"❌ BROKEN — {n} engine gap/wrong value(s) to fix{tail}"
    elif status == "INCOMPLETE":
        verdict = f"⚠️ INCOMPLETE — {len(v.missing_data)} required cell(s) need Bloomberg/market data"
    else:
        verdict = "✅ PASS"
    lines.append(f"- **Coverage status: {verdict}**")
    lines.append(f"- Sanity checks: {sum(c.ok for c in v.checks)}/{len(v.checks)} passed")
    lines.append("")

    if v.sentinels:
        lines.append("## ❌ Wrong values (engine bugs — must fix)")
        lines.append("")
        for s in v.sentinels:
            lines.append(f"- {s}")
        lines.append("")
    if v.missing_engine:
        lines.append("## ❌ Missing required cells (engine gaps — should derive from XBRL)")
        lines.append("")
        for m in v.missing_engine:
            lines.append(f"- {m}")
        lines.append("")
    if v.missing_data:
        lines.append("## ⚠️ Pending Bloomberg/market data (required, user-supplied)")
        lines.append("")
        for m in v.missing_data:
            lines.append(f"- {m}")
        lines.append("")

    if v.notes:
        lines.append("## ℹ️ Not-meaningful values (expected — not blocking)")
        lines.append("")
        for n in v.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines.append("## Coverage checks")
    lines.append("")
    lines.append("| Check | Result | Detail |")
    lines.append("|---|---|---|")
    for c in v.checks:
        lines.append(f"| {c.name} | {'✅' if c.ok else '⚠️'} | {c.detail} |")
    lines.append("")

    if ident:
        lines.append("## Fundamentals accounting identities (filings)")
        lines.append("")
        lines.append("| Rule | Result | Message |")
        lines.append("|---|---|---|")
        for r in ident:
            lines.append(f"| {getattr(r, 'rule', '?')} | {'✅' if getattr(r, 'passed', False) else '⚠️'} "
                         f"| {getattr(r, 'message', '')} |")
        lines.append("")

    if v.blank_cells:
        lines.append("## Blank cells (manual worklist)")
        lines.append("")
        for b in v.blank_cells:
            lines.append(f"- {b}")
        lines.append("")

    if model.warnings:
        lines.append("## Engine warnings")
        lines.append("")
        for w in model.warnings:
            lines.append(f"- {w}")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out
