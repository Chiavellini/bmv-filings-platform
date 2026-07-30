"""AI reader: a document-level read of one quarterly report, as a signal channel.

Spec frozen in study.yaml `hybrid.reader` / `hybrid.contamination` and the prompt
surface in configs/ai_reader.yaml, both committed BEFORE the first read ran.

The channel exists because the analyst-edge decomposition (outputs/ANALYST_EDGE.md)
found the analysts' edge is text-encapsulated: eight outcome-blinded readers
reproduced their call 8/8 from the MD&A alone, using information — FX-vs-organic
revenue, one-offs distorting the comparison, adjusted-vs-reported margins, forward
guidance — that no structured feature carries.

Two hard rules run through this module:

* **Point in time.** A prompt may contain only what was public before the event's
  ``info_dt``. The history table is filtered through the study's own availability
  map; there is no path that reads a later quarter.
* **Channel independence.** The reader never sees the system's scores (s_ts, s_cs,
  SUEs), the desk's estimates, or the analyst's call. The cascade in
  ``earnlib.hybrid`` treats AI and system as separate votes, which is only
  meaningful if they are separately produced.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from earnlib import bootstrap as bs
from earnlib.mdutil import md_table

READER_CFG_PATH = bs.EARNINGS_ROOT / "configs" / "ai_reader.yaml"
CACHE_DIR = bs.EARNINGS_ROOT / "data" / "ai_cache"

# Reader backends.
#   api       the frozen spec — a pinned model at temperature 0, so a re-run
#             returns byte-identical records from the content-addressed cache.
#   subagent  the same prompts routed through outcome-blinded subagents on a
#             Claude subscription. Same prompt, same schema, same cache format,
#             but sampling is not under our control, so re-runs are NOT
#             reproducible. Recorded as a deviation wherever it is used.
# The two carry different cache identities, so their reads can never be
# silently mixed or mistaken for one another.
SUBAGENT_PREFIX = "subagent:"
SUBAGENT_TEMPERATURE = -1.0       # sentinel: sampling not pinned


def backend_identity(block: dict, backend: str) -> tuple[str, float]:
    """(model, temperature) used for the cache key under a given backend."""
    if backend == "subagent":
        return (SUBAGENT_PREFIX + str(block["model"]), SUBAGENT_TEMPERATURE)
    if backend == "api":
        return (str(block["model"]), float(block["temperature"]))
    raise ValueError(f"unknown reader backend {backend!r}")

# Columns the reader sees. Deliberately excludes every model score (s_ts, s_cs,
# sue_*), every desk estimate (est_*, beat_*) and every debt-free derived signal
# the system trades on — the AI channel must not be a re-reading of the system.
HISTORY_COLUMNS = [
    ("period", "quarter", "raw"),
    ("revenue", "revenue", "amount"),
    ("yoy_rev_pct", "rev YoY%", "pct"),
    ("yoy_rev_trail8_mean", "trail8 YoY%", "pct"),
    ("rev_accel_pp", "accel pp", "pct"),
    ("ebitda_margin", "EBITDA m%", "pct"),
    ("ebitda_margin_yoy_pp", "EBITDA m YoY pp", "pct"),
    ("net_margin", "net m%", "pct"),
    ("net_debt", "net debt", "amount"),
    ("nd_to_ebitda_ttm", "ND/EBITDA", "ratio"),
]

# Availability fallback for metric quarters that never produced a dated event —
# the phase_d convention (study.yaml): treat them as public at period end + 90d.
AVAILABILITY_FALLBACK_DAYS = 90

# Context guard. Generous enough that it should never bind (the largest covered
# MD&A is ~100k characters); every truncation is logged and reported.
MAX_MDNA_CHARS = 400_000

_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "setiembre", "octubre", "noviembre", "diciembre",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ReaderConfig:
    raw: dict
    sha256: str

    @property
    def scrub(self) -> dict:
        return self.raw["scrub"]

    @property
    def reason_codes(self) -> list[str]:
        return list(self.raw["reason_codes"])


def load_reader_cfg(path: Path = READER_CFG_PATH) -> ReaderConfig:
    """Parse configs/ai_reader.yaml and hash it — the hash keys the cache, so a
    prompt edit can never silently reuse reads made under the old prompt."""
    import yaml

    blob = path.read_bytes()
    return ReaderConfig(raw=yaml.safe_load(blob.decode()),
                        sha256=hashlib.sha256(blob).hexdigest())


# --------------------------------------------------------------------------
# document text
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_DROP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def html_to_text(raw: str) -> str:
    """Tags out, entities resolved, whitespace collapsed. The MD&A HTML is a
    single flat blob of XBRL-embedded prose; nothing structural survives it."""
    text = _DROP_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _estate_fallback(slug: str, period: str) -> Path | None:
    """Period-named press release in the document-estate view, when the issuer
    filed no MD&A. Prefers the plain name over project-suffixed collisions."""
    d = bs.ESTATE_VIEW_DIR / slug
    if not d.is_dir():
        return None
    hits = sorted((p for p in d.glob(f"{period}*.md") if " " not in p.name),
                  key=lambda p: (len(p.name), p.name))
    return hits[0] if hits else None


def load_document(slug: str, ticker: str, period: str) -> tuple[str, str]:
    """(text, source) for one report. soft's MD&A first — the case reads found
    the alpha-go corpus paths stale for oma/volaris — then the estate release."""
    mdna = (bs.SOFT_ROOT / "data" / "reports" / slug / "xbrl"
            / f"{ticker}_{period}_mdna.html")
    if mdna.exists():
        text = html_to_text(mdna.read_text(errors="ignore"))
        if text:
            return text, "mdna"
    alt = _estate_fallback(slug, period)
    if alt is not None:
        text = alt.read_text(errors="ignore")
        if text.strip():
            return text.strip(), f"estate:{alt.name}"
    return "", "missing"


# --------------------------------------------------------------------------
# point-in-time history
# --------------------------------------------------------------------------

def period_end(period: str) -> pd.Timestamp:
    q = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
    return pd.Timestamp(period[:4] + q[period[5]])


def availability(avail_map: pd.Series) -> dict[tuple[str, str], pd.Timestamp]:
    """(slug, period) -> tz-naive availability timestamp, with the phase_d
    period-end+90d fallback applied by the caller for unmapped quarters."""
    out = {}
    for key, ts in avail_map.items():
        ts = pd.Timestamp(ts)
        out[key] = ts.tz_localize(None) if ts.tzinfo is not None else ts
    return out


def available_at(slug: str, period: str,
                 avail: dict[tuple[str, str], pd.Timestamp]) -> pd.Timestamp:
    hit = avail.get((slug, period))
    if hit is not None:
        return hit
    return period_end(period) + pd.Timedelta(days=AVAILABILITY_FALLBACK_DAYS)


def kpi_history(kpi: pd.DataFrame, slug: str, period: str,
                avail: dict[tuple[str, str], pd.Timestamp],
                max_quarters: int = 20) -> pd.DataFrame:
    """The issuer's own reported history, strictly as of just before its event.

    A quarter appears only when its availability timestamp precedes this event's.
    This is the guard that stops kpi_panel — which is NOT point-in-time — from
    leaking the current or a later quarter into the prompt.
    """
    cutoff = available_at(slug, period, avail)
    rows = kpi[kpi["slug"] == slug].copy()
    keep = [r for _, r in rows.iterrows()
            if available_at(slug, r["period"], avail) < cutoff]
    labels = [label for _, label, _ in HISTORY_COLUMNS]
    if not keep:
        return pd.DataFrame(columns=labels)
    hist = pd.DataFrame(keep).sort_values("period").tail(max_quarters)
    out = pd.DataFrame({label: hist[col].map(_fmt(kind))
                        for col, label, kind in HISTORY_COLUMNS
                        if col in hist.columns})
    return out.reset_index(drop=True)


def _fmt(kind: str):
    """Analyst-readable cells. md_table's default 4-significant-digit float
    formatting renders revenue as '1.041e+04', which is not what anyone reads a
    P&L in."""
    def render(value) -> str:
        if kind == "raw":
            return str(value)
        try:
            x = float(value)
        except (TypeError, ValueError):
            return ""
        if not np.isfinite(x):
            return ""
        if kind == "amount":
            return f"{x:,.0f}"
        if kind == "ratio":
            return f"{x:.2f}x"
        return f"{x:+.1f}" if kind == "pct" else f"{x:.2f}"
    return render


# --------------------------------------------------------------------------
# entity scrubber (arm B)
# --------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def normalize_punct(s: str) -> str:
    """Typographic quotes to ASCII. Reports write "Sam’s Club" with U+2019 while
    a config alias is typed with an apostrophe; without this they never match."""
    return (s.replace("’", "'").replace("‘", "'")
             .replace("“", '"').replace("”", '"')
             .replace("–", "-").replace("—", "-"))


def alias_forms(alias: str) -> set[str]:
    """The alias plus its accent-folded form — reports spell 'Aeroméxico' and
    'Aeromexico' interchangeably."""
    alias = normalize_punct(alias)
    return {alias, _strip_accents(alias)} - {""}


def build_aliases(cfg: ReaderConfig, slug: str, ticker: str) -> list[str]:
    """Every string that would identify this issuer, longest first so that
    'Coca-Cola FEMSA' is replaced before 'FEMSA' can match inside it."""
    names: set[str] = set()
    for alias in cfg.scrub.get("aliases", {}).get(slug, []):
        names |= alias_forms(str(alias))
    names |= alias_forms(ticker)
    for token in slug.replace("_", " ").split():
        if len(token) > 3:
            names |= alias_forms(token)
    return sorted(names, key=lambda s: (-len(s), s))


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

# A personal name: two to five capitalized tokens, allowing the Spanish
# connectives that sit inside compound surnames. They must be able to REPEAT
# and to interleave — "Rodrigo de la Maza" carries two in a row — and the name
# must still end on a capitalized token, never on a dangling "de".
_CAP = r"[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+"
_CONN = r"(?:de|del|la|las|los|y|van|von|di)"
_NAME = rf"{_CAP}(?:(?:\s+{_CONN})*\s+{_CAP}){{1,4}}"


def _person_patterns(titles: list[str]) -> list[tuple[re.Pattern, str]]:
    """Name-next-to-title patterns, in both orders.

    Officer names leak the issuer as surely as its brands do — the pilot probes
    named companies off signature and IR-contact blocks. Anchoring on a title
    keeps this narrow: an unanchored capitalized-name matcher would eat product
    names, place names and section headings, and gut the document. The title is
    captured rather than looked behind, because a multi-word title is not
    fixed-width; it is re-emitted unchanged, since "Director General" identifies
    nobody on its own.
    """
    out: list[tuple[re.Pattern, str]] = []
    for title in sorted(titles, key=len, reverse=True):
        t = r"\s+".join(re.escape(p) for p in str(title).split())
        sep = r"[\s:,.\-–]{1,6}"
        out.append((re.compile(
            rf"(?P<title>{t})(?P<sep>{sep})(?P<name>{_NAME})"), "title_first"))
        out.append((re.compile(
            rf"(?P<name>{_NAME})(?P<sep>{sep})(?P<title>{t})"), "name_first"))
    return out


def _alias_pattern(alias: str) -> re.Pattern:
    """Word-bounded, plural-tolerant, whitespace-flexible. Short ALL-CAPS
    aliases match case SENSITIVELY: several tickers are ordinary Spanish or
    English words — HOTEL, NEXT, GAP, ARA — and folding case would scrub the
    vocabulary a hotel or airport issuer needs to describe its own business."""
    generic = alias.isupper() and len(alias) <= 5
    flags = 0 if generic else re.I
    body = r"\s+".join(re.escape(part) for part in alias.split())
    return re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)", flags)


def scrub_text(text: str, cfg: ReaderConfig, slug: str, ticker: str,
               period: str) -> tuple[str, dict]:
    """Arm B: identity out, arithmetic intact.

    Replaces issuer names, brands and signature assets with placeholders; makes
    years and quarter labels RELATIVE to the reported period (so year-over-year
    prose still reads) and month names generic. Numbers are never altered.
    """
    sc = cfg.scrub
    issuer = sc.get("issuer_placeholder", "THE COMPANY")
    stats = {"alias_hits": 0, "year_hits": 0, "quarter_hits": 0, "month_hits": 0,
             "url_hits": 0, "person_hits": 0}

    text = normalize_punct(text)
    # URLs and mailboxes carry the issuer's name past every word boundary
    # ("tinyurl.com/Becle3Q25ConferenceCall", "ir@walmex.com.mx").
    text, n = _URL_RE.subn("[URL]", text)
    stats["url_hits"] += n
    text, n = _EMAIL_RE.subn("[EMAIL]", text)
    stats["url_hits"] += n

    for alias in build_aliases(cfg, slug, ticker):
        text, n = _alias_pattern(alias).subn(issuer, text)
        stats["alias_hits"] += n

    year_t = int(period[:4])
    span = int(sc.get("relative_year_span", 6))
    for offset in range(-span, 2):
        year = year_t + offset
        label = "YEAR_T" if offset == 0 else f"YEAR_T{offset:+d}"
        # a bare 4-digit token. Amounts carry a separator or a decimal tail, so
        # only those are excluded — a trailing period or comma is punctuation.
        text, n = re.subn(rf"(?<![\d,.]){year}(?!\d)(?!\.\d)", label, text)
        stats["year_hits"] += n
        short = f"{year % 100:02d}"
        for q in range(1, 5):
            qlabel = _relative_quarter(period, year, q)
            for pattern in (rf"(?<!\w){q}[TQ]{short}(?!\d)",
                            rf"(?<!\w){q}[TQ]{year}(?!\d)"):
                text, n = re.subn(pattern, qlabel, text, flags=re.I)
                stats["quarter_hits"] += n

    if sc.get("strip_month_names", True):
        for month in _MONTHS:
            text, n = re.subn(rf"(?<!\w){month}(?!\w)", "[MONTH]", text, flags=re.I)
            stats["month_hits"] += n

    if sc.get("redact_person_names", False):
        person = sc.get("person_placeholder", "[PERSON]")
        for pattern, order in _person_patterns(sc.get("person_titles", [])):
            def repl(m, order=order):
                if order == "title_first":
                    return f"{m.group('title')}{m.group('sep')}{person}"
                return f"{person}{m.group('sep')}{m.group('title')}"
            text, n = pattern.subn(repl, text)
            stats["person_hits"] += n

    return text, stats


def _relative_quarter(period: str, year: int, q: int) -> str:
    """Quarter label relative to the reported one: the current quarter becomes
    QUARTER_T, the year-ago quarter QUARTER_T-4."""
    ref = int(period[:4]) * 4 + int(period[5]) - 1
    cur = year * 4 + q - 1
    diff = cur - ref
    return "QUARTER_T" if diff == 0 else f"QUARTER_T{diff:+d}"


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    arm: str
    source: str
    truncated: bool
    scrub_stats: dict


def build_prompt(cfg: ReaderConfig, *, arm: str, slug: str, ticker: str,
                 period: str, sector: str, text: str, source: str,
                 history: pd.DataFrame) -> Prompt:
    """Render one read prompt. ``arm`` is 'N' (named) or 'B' (scrubbed)."""
    if arm not in ("N", "B"):
        raise ValueError(f"arm must be N or B, got {arm!r}")
    stats: dict = {}
    if arm == "B":
        text, stats = scrub_text(text, cfg, slug, ticker, period)
        issuer_label = cfg.scrub.get("issuer_placeholder", "THE COMPANY")
    else:
        issuer_label = f"{ticker} ({slug}), reporting {period}"

    truncated = len(text) > MAX_MDNA_CHARS
    if truncated:
        text = text[:MAX_MDNA_CHARS]

    table = md_table(history) if len(history) else "(no prior quarters available)"
    user = cfg.raw["user"].format(
        issuer_label=issuer_label, sector=sector or "not disclosed",
        kpi_history=table, mdna_text=text,
        reason_codes=", ".join(cfg.reason_codes),
    )
    return Prompt(system=cfg.raw["system"], user=user, arm=arm, source=source,
                  truncated=truncated, scrub_stats=stats)


def probe_prompt(cfg: ReaderConfig, scrubbed_text: str) -> Prompt:
    """The identification probe — run on arm-B text, in its own call, so that
    recognizing the issuer cannot contaminate the read it is measuring."""
    return Prompt(system=cfg.raw["probe_system"],
                  user=cfg.raw["probe_user"].format(mdna_text=scrubbed_text),
                  arm="probe", source="", truncated=False, scrub_stats={})


# --------------------------------------------------------------------------
# cache + call
# --------------------------------------------------------------------------

def cache_key(cfg: ReaderConfig, model: str, temperature: float,
              prompt: Prompt) -> str:
    h = hashlib.sha256()
    for part in (cfg.sha256, model, f"{temperature:.4f}", prompt.arm,
                 prompt.system, prompt.user):
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()


def cache_path(key: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{key}.json"


def call_reader(prompt: Prompt, cfg: ReaderConfig, *, model: str,
                temperature: float, max_tokens: int,
                cache_dir: Path = CACHE_DIR, allow_call: bool = True) -> dict:
    """Cached single completion. Returns the stored record.

    The cache is content-addressed by (reader config hash, model, temperature,
    arm, prompt), so a re-run is byte-identical and a prompt change is a miss
    rather than a silent reuse. ``allow_call=False`` makes the function offline:
    it returns cached records and raises for anything not already read — which is
    how the prospective instrument proves a read predates a report.
    """
    key = cache_key(cfg, model, temperature, prompt)
    path = cache_path(key, cache_dir)
    if path.exists():
        return json.loads(path.read_text())
    if not allow_call:
        raise LookupError(f"no cached read for key {key[:12]} (arm {prompt.arm})")

    # Only the calling path needs a provider. An offline read or a cache hit must not
    # depend on the active extraction engine having llm_providers vendored.
    from src.extract.llm_providers import complete_json, resolve_provider

    provider = resolve_provider({"provider": "anthropic", "model": model})
    if provider is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot run reads")
    response = complete_json(provider, system=prompt.system, user=prompt.user,
                             max_tokens=max_tokens, temperature=temperature)
    record = {
        "key": key, "arm": prompt.arm, "model": model,
        "temperature": temperature, "reader_cfg_sha256": cfg.sha256,
        "source": prompt.source, "truncated": prompt.truncated,
        "prompt_chars": len(prompt.user), "response": response,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    return record


# --------------------------------------------------------------------------
# response validation
# --------------------------------------------------------------------------

_DIRECTION_SIGN = {"up": 1, "down": -1, "flat": 0}


def validate(response: dict, cfg: ReaderConfig) -> tuple[dict, list[str]]:
    """(normalized fields, problems). A malformed read is neutralized, never
    guessed at: an unusable direction becomes flat, which the cascade abstains
    on. Problems are counted and reported rather than silently dropped."""
    schema = cfg.raw["schema"]
    problems: list[str] = []
    out: dict = {}

    for field, allowed in schema["enums"].items():
        value = response.get(field)
        value = str(value).strip().lower() if value is not None else None
        if value not in allowed:
            problems.append(f"{field}={response.get(field)!r}")
            value = "flat" if field == "direction" else None
        out[field] = value

    code = response.get("reason_code")
    if code not in cfg.reason_codes:
        problems.append(f"reason_code={code!r}")
        code = "otro"
    out["reason_code"] = code
    secondary = response.get("reason_code_secondary")
    out["reason_code_secondary"] = secondary if secondary in cfg.reason_codes else None

    for field in ("headline_rev_growth_pct", "organic_constant_fx_growth_pct",
                  "adjusted_vs_reported_ebitda_gap_pct"):
        out[field] = _as_float(response.get(field))

    items = response.get("one_off_items")
    out["one_off_items"] = items if isinstance(items, list) else []
    out["n_one_offs"] = len(out["one_off_items"])
    out["n_material_one_offs"] = sum(
        1 for it in out["one_off_items"]
        if isinstance(it, dict) and it.get("material") is True)
    out["ex_associate_net_income_note"] = _as_text(
        response.get("ex_associate_net_income_note"))
    out["rationale"] = _as_text(response.get("rationale"))
    out["ai_dir"] = _DIRECTION_SIGN.get(out.get("direction") or "flat", 0)
    return out, problems


def _as_float(value) -> float:
    try:
        if value is None or isinstance(value, bool):
            return float("nan")
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _as_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_probe(response: dict, ticker: str) -> dict:
    """Did the reader recognize the issuer, and does it remember the outcome?

    Identification is scored on the ticker or the issuer name appearing in the
    answer; deliberately generous, because the gate is more conservative when
    more events count as identified.
    """
    said_ticker = str(response.get("ticker") or "").strip().upper()
    said_company = _strip_accents(str(response.get("company") or "")).lower()
    identified = bool(
        said_ticker and said_ticker != "UNKNOWN"
        and (said_ticker == ticker.upper() or ticker.upper() in said_ticker)
    ) or (said_company not in ("", "unknown")
          and _strip_accents(ticker).lower() in said_company)
    move = str(response.get("next_day_move") or "unknown").strip().lower()
    return {
        "identified": identified,
        "probe_ticker": said_ticker or None,
        "probe_company": _as_text(response.get("company")),
        "probe_confidence": _as_text(response.get("confidence")),
        "probe_move": move if move in ("up", "down") else "unknown",
        "probe_basis": _as_text(response.get("recall_basis")),
    }


def sign_series(values: pd.Series) -> np.ndarray:
    return np.sign(values.to_numpy(dtype=float))
