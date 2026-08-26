"""
KC Options Ingest — LSEG (interim migration)
================================================
LSEG-API replacement for ICEBREAKER/Options/Code/Ingest.py (icepython-based).
Same final schema as the ICE source so Dashboard/app.py (copied verbatim)
works unchanged: date, settle, oi, volume, ric, option_type, strike,
expiry_month, expiry_year, impvol.

RIC scheme confirmed directly against LSEG (both via discovery.search and a
live get_history call): 1KC<strike*100><month_code><2-digit year>, e.g.
1KC35000J26 = KC Oct 2026 350.00 Call. Standard OCC-style month-letter
scheme: A-L = Jan-Dec calls, M-X = Jan-Dec puts. This mirrors the RIC logic
already proven in the sibling (unfinished) LSEG prototype at
Non Fundamental/Options/Code/ingest.py, cross-checked live here.

IMPORTANT — do not add a T+1 OI/Volume shift. The ICE source shifts OI and
Volume back one row per RIC to compensate for a T+1 ICE publishing quirk.
Per direct instruction, LSEG's OI/Volume are used exactly as published, no
shift applied, even though this means the two sources' OI/Volume columns
will show a one-day offset from each other if ever compared directly.

Only currently-active (non-expired) contracts are queryable via
get_history — this was confirmed empirically (an expired Sep-2026 contract
returned "universe not found" for history while a live Oct-2026 contract at
the same strike returned clean daily data). Since the strike/month universe
is built fresh from today's date on every run, this is a non-issue in
practice — the script never asks for an expired contract's history.

UNIVERSE — discovery vs the old ATM window
------------------------------------------
The universe used to be *guessed*: ATM +/- STRIKE_STEPS * STRIKE_GAP (KC: ATM
+/- 50c -> 41 strikes) x 12 forward months. Measured against LSEG's actual
listed board on 2026-08-25 that captured 159 of 1,730 live monthly RICs and
only 27% of open interest — 148,725 lots sat on strikes outside the window
versus 54,940 inside it, overwhelmingly deep OTM puts (200P 11,313 lots,
230P 10,806, 250P 10,652, 220P 9,319 ...). Those are the producer-hedge
strikes, so the OI dashboard was blind to most of the board.

The universe is now DISCOVERED from LSEG search (DERIVATIVE_QUOTES view,
startswith(RIC,'1KC')), which returns every listed option with its strike,
put/call flag and expiry. --legacy-window falls back to the old arithmetic
window, and discovery failure falls back automatically.

WEEKLIES ARE DELIBERATELY EXCLUDED. LSEG also lists weekly/serial options
under a different RIC shape — 1KC<n>W<strike*100><code><single-digit-year>,
e.g. 1KC1W24000I6 — alongside the monthly 1KC<strike*100><code><yy>. Both
collapse to the same (strike, expiry_month, expiry_year) key that this
parquet and the dashboard pivot on, so ingesting them would let a weekly
silently overwrite a monthly under pivot_table(aggfunc="first"). They also
carry no open interest. Pass --weeklies to include them anyway (the extra
rows are tagged in the `series` column so downstream can separate them).

Usage:
    python kc_ingest_lseg.py                  # incremental (10-day rolling upsert)
    python kc_ingest_lseg.py --full           # backfill BACKFILL_DAYS then upsert
    python kc_ingest_lseg.py --legacy-window  # old ATM +/- window universe
    python kc_ingest_lseg.py --dry-run        # report universe/coverage, write nothing
"""

import argparse
import datetime
import json
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
pd.set_option("future.no_silent_downcasting", True)  # silences a harmless lseg.data internal FutureWarning

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# A dedicated named logger (not logging.basicConfig on root) — basicConfig
# attaches handlers to the root logger, which also catches lseg.data's
# internal httpx request logging ("HTTP Request: GET ..." per RIC) since
# that propagates to root by default. That bloated the automator's status
# email to tens of thousands of lines. A named logger avoids the leak.
_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kc_ingest_lseg")
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:
    _sh = logging.StreamHandler(sys.stdout); _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(LOG_DIR / "kc_ingest_lseg.log", encoding="utf-8"); _fh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.addHandler(_fh)
logging.getLogger("httpx").setLevel(logging.WARNING)

DB_DIR       = Path(__file__).parent.parent / "Database"
DASH_DIR     = Path(__file__).parent.parent / "Dashboard"
PARQUET_PATH = DB_DIR / "KC_options_ice.parquet"   # same filename ICE's Dashboard expects
ATM_JSON     = DASH_DIR / "atm.json"

COMMODITY      = "KC"
ATM_RIC        = "KCc1"       # front-month continuation, matches ICE's "KC 1!" convention
STRIKE_GAP     = 2.5
STRIKE_STEPS   = 20           # legacy window only: ATM +/- 20 -> 41 strikes
MONTHS_FORWARD = 12           # legacy window only
BACKFILL_DAYS  = 90
ROLLING_DAYS   = 10            # matches the ICE source's rolling re-fetch window
BATCH_SIZE     = 50
PREFILTER_SIZE = 100
FETCH_RETRIES  = 3             # transient LSEG timeouts are common on wide universes
FETCH_BACKOFF  = 5             # seconds, doubled per retry
ACTIVE_LOOKBACK = 10           # days of OI/volume history that make a RIC "active"

# Measured cost model (2026-08-26). lseg.data does NOT batch get_history: it
# issues one HTTP request per RIC to the local Workspace proxy, so wall time is
# linear in RIC COUNT and almost independent of everything else.
#   batch size 50 / 100 / 200 over 400 RICs -> 82.7s / 82.8s / 83.0s  (no effect)
#   window 10d vs 90d at 400 RICs           -> 83.4s / 83.7s          (payload ~free)
#   400 RICs -> 83s  =>  0.2075 s/RIC; 1,730 RICs predicted 359s, measured 358s
#   concurrency (2 and 4 threads)           -> read timeouts, batches failed
# So neither bigger batches, shorter windows nor threading help. The only lever
# is fetching fewer RICs — which is what --active-only does. Corollary: because
# payload is nearly free, a full sweep should use a LONG window rather than a
# short one, since the extra history costs almost nothing.
FIELDS         = ["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"]

SEARCH_PREFIX  = f"1{COMMODITY}"
SEARCH_TOP     = 10000
SEARCH_NAME_RE = "Coffee"      # guards against same-prefix equities (e.g. 1KCR.MI)

CALL_CODES = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
PUT_CODES  = {1:"M",2:"N",3:"O",4:"P",5:"Q",6:"R",7:"S",8:"T",9:"U",10:"V",11:"W",12:"X"}
MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}

CODE_TO_CALL_MONTH = {v: k for k, v in CALL_CODES.items()}
CODE_TO_PUT_MONTH  = {v: k for k, v in PUT_CODES.items()}

# 1KC30000J26  -> strike*100, month code, 2-digit year   (monthly / standard)
MONTHLY_RE = re.compile(rf"^1{COMMODITY}(\d+)([A-X])(\d{{2}})$")
# 1KC1W24000I6 -> week no, strike*100, month code, 1-digit year  (weekly / serial)
WEEKLY_RE  = re.compile(rf"^1{COMMODITY}(\d)W(\d+)([A-X])(\d)$")

today = pd.Timestamp.today().normalize()


# ── Universe construction ────────────────────────────────────────────────────

def get_atm_strike(ld) -> float:
    """Front-month price snapped to the strike grid.

    Retries, and falls back to SETTLE, because this is the first call of the
    run: an unretried timeout here aborts the whole ingest before a single row
    is fetched (observed 2026-08-26). TRDPRC_1 also goes null off-hours, which
    the sibling _common.get_atm_strike already guards against.
    """
    delay = FETCH_BACKOFF
    last = None
    for attempt in range(1, FETCH_RETRIES + 1):
        for field in ("TRDPRC_1", "SETTLE"):
            try:
                df = ld.get_data(universe=[ATM_RIC], fields=[field])
                price = df[field].iloc[0]
                if not pd.isna(price):
                    if field != "TRDPRC_1":
                        log.info("ATM from %s (TRDPRC_1 unavailable)", field)
                    return round(round(float(price) / STRIKE_GAP) * STRIKE_GAP, 2)
            except Exception as e:
                last = e
        if attempt < FETCH_RETRIES:
            log.warning("ATM fetch failed (attempt %d/%d): %s — retrying in %ds",
                        attempt, FETCH_RETRIES, str(last)[:100], delay)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"{ATM_RIC}: no ATM price after {FETCH_RETRIES} attempts ({str(last)[:120]})")


def build_strikes(atm: float) -> list:
    return [round(atm + i * STRIKE_GAP, 2) for i in range(-STRIKE_STEPS, STRIKE_STEPS + 1)]


def build_months() -> list:
    months, m, y = [], today.month, today.year
    for _ in range(MONTHS_FORWARD):
        months.append((m, y))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def build_ric(strike: float, month: int, year: int, option_type: str) -> str:
    code = CALL_CODES[month] if option_type == "Call" else PUT_CODES[month]
    return f"1{COMMODITY}{int(round(strike * 100))}{code}{str(year)[-2:]}"


def build_meta(strikes: list, months: list) -> pd.DataFrame:
    rows = []
    for strike in strikes:
        for (m, y) in months:
            for otype in ("Call", "Put"):
                rows.append({
                    "ric": build_ric(strike, m, y, otype),
                    "option_type": otype,
                    "strike": strike,
                    "expiry_month": m,
                    "expiry_year": y,
                    "series": "monthly",
                })
    return pd.DataFrame(rows)


def parse_ric(ric: str):
    """Decode an LSEG KC option RIC into its contract fields.

    Returns None for anything that is not a KC option (the search prefix also
    matches unrelated instruments such as the equity 1KCR.MI).

    Note the month code is the CONTRACT month, not the expiry date: LSEG lists
    1KC25000X26 as 'Coffee C ... 250 Put Dec 2026' with ExpiryDate 2026-11-12,
    because coffee options expire the month before the futures month. The
    existing parquet and the dashboard key on the contract month, so that is
    what is stored here.
    """
    m = MONTHLY_RE.match(ric)
    if m:
        raw, code, yy = m.group(1), m.group(2), m.group(3)
        series, year = "monthly", 2000 + int(yy)
    else:
        w = WEEKLY_RE.match(ric)
        if not w:
            return None
        raw, code, y1 = w.group(2), w.group(3), w.group(4)
        # single-digit year: resolve to the nearest year ending in that digit
        base = today.year - (today.year % 10)
        year = base + int(y1)
        if year < today.year - 1:
            year += 10
        series = f"weekly{w.group(1)}"

    if code in CODE_TO_CALL_MONTH:
        otype, month = "Call", CODE_TO_CALL_MONTH[code]
    elif code in CODE_TO_PUT_MONTH:
        otype, month = "Put", CODE_TO_PUT_MONTH[code]
    else:
        return None

    return {"ric": ric, "option_type": otype, "strike": int(raw) / 100.0,
            "expiry_month": month, "expiry_year": year, "series": series}


def discover_meta(ld, include_weeklies: bool = False) -> pd.DataFrame:
    """Enumerate the live listed option board from LSEG search.

    Replaces the guessed ATM +/- window. Returns the same meta columns as
    build_meta() so the rest of the pipeline is unchanged.
    """
    from lseg.data.content import search

    r = search.Definition(
        view=search.Views.DERIVATIVE_QUOTES,
        filter=f"startswith(RIC,'{SEARCH_PREFIX}') and ExpiryDate ne null",
        select="RIC,DTSubjectName,ExpiryDate,StrikePrice,CallPutOption",
        top=SEARCH_TOP,
    ).get_data()
    d = r.data.df
    if d is None or d.empty:
        raise RuntimeError("search returned no rows")
    if len(d) >= SEARCH_TOP:
        log.warning("search hit the %d row cap — universe may be truncated", SEARCH_TOP)

    d = d.copy()
    d["ExpiryDate"] = pd.to_datetime(d["ExpiryDate"], errors="coerce")
    d["StrikePrice"] = pd.to_numeric(d["StrikePrice"], errors="coerce")
    name = d["DTSubjectName"].astype(str)
    d = d[name.str.contains(SEARCH_NAME_RE, case=False, na=False)]
    log.info("Search: %d rows after %s name filter", len(d), SEARCH_NAME_RE)

    # keep only contracts that have not expired, and drop the strike=0 artifacts
    live = d[(d["ExpiryDate"] > today) & (d["StrikePrice"] > 0)]
    log.info("Search: %d live (unexpired, strike>0)", len(live))

    parsed = [p for p in (parse_ric(x) for x in live["RIC"].unique()) if p]
    meta = pd.DataFrame(parsed)
    if meta.empty:
        raise RuntimeError("no RICs parsed from search results")

    n_week = int((meta["series"] != "monthly").sum())
    if include_weeklies:
        log.info("Universe: %d monthly + %d weekly/serial (weeklies INCLUDED)",
                 len(meta) - n_week, n_week)
    else:
        meta = meta[meta["series"] == "monthly"].reset_index(drop=True)
        log.info("Universe: %d monthly (excluded %d weekly/serial — they collide "
                 "with monthlies on (strike, month, year))", len(meta), n_week)

    # cross-check the strike we decoded from the RIC against search metadata
    chk = meta.merge(live[["RIC", "StrikePrice", "CallPutOption"]].drop_duplicates("RIC"),
                     left_on="ric", right_on="RIC", how="left")
    bad = chk[chk["StrikePrice"].notna() &
              ((chk["StrikePrice"] - chk["strike"]).abs() > 1e-6)]
    if len(bad):
        log.warning("%d RICs where decoded strike != search strike, e.g. %s",
                    len(bad), bad[["ric", "strike", "StrikePrice"]].head(3).to_dict("records"))
    bad_cp = chk[chk["CallPutOption"].notna() & (chk["CallPutOption"] != chk["option_type"])]
    if len(bad_cp):
        log.warning("%d RICs where decoded call/put != search, e.g. %s",
                    len(bad_cp), bad_cp[["ric", "option_type", "CallPutOption"]].head(3).to_dict("records"))
    if not len(bad) and not len(bad_cp):
        log.info("RIC decode cross-checked against search metadata: all %d agree", len(chk))

    return meta[["ric", "option_type", "strike", "expiry_month", "expiry_year", "series"]]


# ── Fetch ─────────────────────────────────────────────────────────────────────

def prefilter_live(ld, rics: list, require_oi: bool = False) -> list:
    """Snapshot-check the board in batches and keep only instruments that are
    actually quoted, before the more expensive historical pull.

    The original kept OI > 0 only. That is too tight once the universe is
    discovered rather than guessed: on 2026-08-25, 1,722 of 1,730 live KC
    monthlies carried a settle but only 484 carried OI, and the settle-only
    contracts are exactly the wings the vol surface and Px Change panels need.
    Keeping "OI > 0 OR settle present" preserves those without pulling history
    for genuinely dead RICs. Pass require_oi=True for the old behaviour.
    """
    live, seen_oi, seen_px = [], 0, 0
    for i in range(0, len(rics), PREFILTER_SIZE):
        batch = rics[i:i + PREFILTER_SIZE]
        try:
            df = ld.get_data(universe=batch, fields=["OPINT_1", "SETTLE"])
        except Exception as e:
            log.warning("  prefilter batch failed: %s", str(e)[:120])
            continue
        if df is None or df.empty:
            continue
        oi = pd.to_numeric(df.get("OPINT_1"), errors="coerce").fillna(0)
        px = pd.to_numeric(df.get("SETTLE"), errors="coerce")
        seen_oi += int((oi > 0).sum())
        seen_px += int(px.notna().sum())
        keep = (oi > 0) if require_oi else ((oi > 0) | px.notna())
        live.extend(df[keep]["Instrument"].tolist())
    log.info("  prefilter: %d with OI>0, %d with settle -> %d kept",
             seen_oi, seen_px, len(live))
    return live


def fetch_batch(ld, rics: list, start: str, end: str, retries: int = FETCH_RETRIES):
    """Fetch a batch of RICs. Returns (dataframe, definitive).

    `definitive` distinguishes "LSEG says there is no such data" from "the call
    failed". That matters because the incremental upsert drops the existing rows
    inside the refresh window before writing the new ones — so a transient
    timeout treated as an empty result silently deletes history. A measured
    incremental run lost 600 rows to exactly that: 3 of 35 batches timed out and
    were recorded as "no data". Failed batches are retried with backoff, and any
    that still fail are reported so the caller can protect their existing rows.
    """
    delay = FETCH_BACKOFF
    for attempt in range(1, retries + 1):
        try:
            df = ld.get_history(universe=rics, fields=FIELDS, start=start, end=end, interval="daily")
        except Exception as e:
            err = str(e)
            if "not found" in err.lower() or "70005" in err:
                return pd.DataFrame(), True      # genuinely absent
            if attempt < retries:
                log.warning("  batch error (attempt %d/%d): %s — retrying in %ds",
                            attempt, retries, err[:110], delay)
                time.sleep(delay)
                delay *= 2
                continue
            log.error("  batch FAILED after %d attempts: %s", retries, err[:150])
            return pd.DataFrame(), False         # could not be established
        break

    if df is None or df.empty:
        return pd.DataFrame(), True

    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    col_map = {"SETTLE": "settle", "OPINT_1": "oi", "ACVOL_UNS": "volume", "IMP_VOLT": "impvol"}
    parts = []

    if isinstance(df.columns, pd.MultiIndex):
        for ric in df.columns.get_level_values(0).unique():
            sub = df[ric].copy().dropna(how="all")
            if sub.empty:
                continue
            sub = sub.rename(columns=col_map)
            sub["ric"] = ric
            parts.append(sub.reset_index())
    else:
        sub = df.copy().dropna(how="all")
        if not sub.empty:
            sub = sub.rename(columns=col_map)
            sub["ric"] = rics[0]
            parts.append(sub.reset_index())

    return (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()), True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help=f"Backfill {BACKFILL_DAYS} days instead of incremental")
    parser.add_argument("--days", type=int, default=None, help="Override the fetch window in days")
    parser.add_argument("--legacy-window", action="store_true",
                        help="Use the old ATM +/- window universe instead of LSEG discovery")
    parser.add_argument("--weeklies", action="store_true",
                        help="Also ingest weekly/serial options (collide with monthlies on (strike,month,year))")
    parser.add_argument("--require-oi", action="store_true",
                        help="Prefilter to OI>0 only (old behaviour); default also keeps settle-quoted strikes")
    parser.add_argument("--active-only", action="store_true",
                        help="Fetch only RICs that carried OI or volume recently (see ACTIVE_LOOKBACK). "
                             "Cuts a daily run from ~6min to ~1.7min; the settle-only wings then refresh "
                             "on whatever cadence you run the full sweep.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report universe and coverage, then exit without writing the parquet")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("KC Options Ingest (LSEG) | %s", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    import lseg.data as ld
    ld.open_session()
    log.info("LSEG session opened.")

    try:
        first_run   = args.full or not PARQUET_PATH.exists()
        window_days = args.days if args.days else (BACKFILL_DAYS if first_run else ROLLING_DAYS)
        fetch_start = (today - pd.Timedelta(days=window_days)).strftime("%Y-%m-%d")
        fetch_end   = today.strftime("%Y-%m-%d")
        log.info("Mode: %s | window: %s -> %s (%dd)",
                 "FULL" if first_run else "INCREMENTAL", fetch_start, fetch_end, window_days)

        atm = get_atm_strike(ld)

        meta = None
        if not args.legacy_window:
            try:
                meta = discover_meta(ld, include_weeklies=args.weeklies)
            except Exception as e:
                log.warning("Discovery failed (%s) — falling back to the ATM window.", str(e)[:160])
                meta = None
        if meta is None:
            strikes = build_strikes(atm)
            months  = build_months()
            meta    = build_meta(strikes, months)
            log.info("ATM (%s): %s | legacy window strikes %s-%s (%d) x %d months",
                     ATM_RIC, atm, strikes[0], strikes[-1], len(strikes), len(months))

        all_rics = meta["ric"].tolist()
        log.info("ATM (%s): %s | candidate RICs: %d | strikes %g-%g (%d distinct) | expiries: %d",
                 ATM_RIC, atm, len(all_rics), meta["strike"].min(), meta["strike"].max(),
                 meta["strike"].nunique(),
                 meta.groupby(["expiry_year", "expiry_month"]).ngroups)

        t0 = time.time()
        live_rics = prefilter_live(ld, all_rics, require_oi=args.require_oi)
        log.info("Quoted RICs: %d / %d (%.0fs)", len(live_rics), len(all_rics), time.time() - t0)

        skipped_rics = []
        if args.active_only and PARQUET_PATH.exists():
            # Wall time is linear in RIC count (see the cost model above), so the
            # only way to make a daily run faster is to ask for fewer RICs. Keep
            # the ones that actually traded or held OI recently; the rest are
            # settle-only wings that carry no positioning and can refresh on the
            # full sweep instead.
            prev = pd.read_parquet(PARQUET_PATH)
            prev["date"] = pd.to_datetime(prev["date"])
            recent = prev[prev["date"] >= prev["date"].max() - pd.Timedelta(days=ACTIVE_LOOKBACK)]
            active = set(recent[pd.to_numeric(recent["oi"], errors="coerce") > 0]["ric"]) | \
                     set(recent[pd.to_numeric(recent["volume"], errors="coerce") > 0]["ric"])
            # anything newly listed has no history yet — always include it
            newly = set(live_rics) - set(prev["ric"])
            trimmed = [r for r in live_rics if r in active or r in newly]
            if trimmed:
                log.info("ACTIVE-ONLY: %d of %d RICs traded or held OI in the last %dd "
                         "(+%d newly listed) — est. %.0fs instead of %.0fs",
                         len(trimmed), len(live_rics), ACTIVE_LOOKBACK, len(newly),
                         len(trimmed) * 0.2075, len(live_rics) * 0.2075)
                # The RICs we are choosing NOT to fetch must be protected exactly
                # like a failed batch: the incremental upsert clears the refresh
                # window before writing, so without this their recent rows would
                # be deleted rather than left alone.
                skipped_rics = [r for r in live_rics if r not in set(trimmed)]
                live_rics = trimmed
            else:
                log.warning("ACTIVE-ONLY matched nothing — falling back to the full quoted set.")

        if not live_rics:
            log.error("No live RICs found — aborting without touching the parquet.")
            sys.exit(1)

        if args.dry_run:
            live_meta = meta[meta["ric"].isin(live_rics)]
            log.info("DRY RUN — would fetch %d RICs, strikes %g-%g (%d distinct)",
                     len(live_rics), live_meta["strike"].min(), live_meta["strike"].max(),
                     live_meta["strike"].nunique())
            if PARQUET_PATH.exists():
                cur = pd.read_parquet(PARQUET_PATH)
                log.info("Current parquet: %d RICs, %d strikes (%g-%g)",
                         cur["ric"].nunique(), cur["strike"].nunique(),
                         cur["strike"].min(), cur["strike"].max())
                gain = sorted(set(live_meta["strike"]) - set(cur["strike"]))
                log.info("Strikes that would be ADDED (%d): %s", len(gain), gain)
            log.info("DRY RUN — nothing written.")
            return

        all_dfs, failed_rics = [], []
        n_batches = (len(live_rics) + BATCH_SIZE - 1) // BATCH_SIZE
        t0 = time.time()
        for i in range(0, len(live_rics), BATCH_SIZE):
            batch = live_rics[i:i + BATCH_SIZE]
            b_num = i // BATCH_SIZE + 1
            df, definitive = fetch_batch(ld, batch, fetch_start, fetch_end)
            if not df.empty:
                all_dfs.append(df)
                log.info("  batch %d/%d: %d rows (%d RICs with data)", b_num, n_batches, len(df), df["ric"].nunique())
            elif definitive:
                log.info("  batch %d/%d: no data", b_num, n_batches)
            else:
                failed_rics.extend(batch)
                log.warning("  batch %d/%d: UNRESOLVED — existing rows for these %d RICs will be preserved",
                            b_num, n_batches, len(batch))
        log.info("History fetch complete in %.0fs", time.time() - t0)
        if failed_rics:
            log.warning("%d RICs could not be fetched this run (%d batches); their history is kept as-is.",
                        len(failed_rics), (len(failed_rics) + BATCH_SIZE - 1) // BATCH_SIZE)

        if not all_dfs:
            log.error("No data returned from any batch.")
            sys.exit(1)

        new_data = pd.concat(all_dfs, ignore_index=True)
        new_data = new_data.merge(meta, on="ric", how="left")
        new_data["date"] = pd.to_datetime(new_data["date"])
        for col in ["settle", "oi", "volume", "impvol"]:
            if col in new_data.columns:
                new_data[col] = pd.to_numeric(new_data[col], errors="coerce")
        new_data = new_data.dropna(subset=["settle"])

        DB_DIR.mkdir(parents=True, exist_ok=True)
        window_start = today - pd.Timedelta(days=window_days)

        if PARQUET_PATH.exists():
            existing = pd.read_parquet(PARQUET_PATH)
            if "series" not in existing.columns:
                # everything ingested before discovery was monthly by construction
                existing["series"] = "monthly"
            existing["date"] = pd.to_datetime(existing["date"])
            if first_run:
                # A backfill must never lose history: keep every existing row and
                # let the freshly fetched window override it on (date, ric).
                # Replacing outright would have silently dropped the days that sit
                # before the backfill window (the parquet started 2026-05-21 while
                # a 90d window from 2026-08-25 only reaches 2026-05-27).
                base = existing
            else:
                # Keep rows outside the refresh window, plus every row belonging to
                # a RIC whose batch could not be fetched — otherwise a transient
                # timeout deletes that RIC's window instead of leaving it alone.
                protect = set(failed_rics) | set(skipped_rics)
                keep = existing["date"] < window_start
                if protect:
                    keep = keep | existing["ric"].isin(protect)
                base = existing[keep]
            final = (pd.concat([base, new_data], ignore_index=True)
                      .drop_duplicates(subset=["date", "ric"], keep="last")
                      .sort_values(["ric", "date"]).reset_index(drop=True))
        else:
            final = (new_data.drop_duplicates(subset=["date", "ric"], keep="last")
                     .sort_values(["ric", "date"]).reset_index(drop=True))

        # `series` records monthly vs weekly provenance. The dashboard reads
        # columns by name so the extra column is inert there, but it is the only
        # way to tell a weekly from a monthly once both are in the file.
        keep_cols = ["date", "settle", "oi", "volume", "ric", "option_type",
                     "strike", "expiry_month", "expiry_year", "impvol", "series"]
        for c in keep_cols:
            if c not in final.columns:
                final[c] = pd.NA
        final = final[keep_cols]
        final["series"] = final["series"].fillna("monthly")
        final["oi"]     = final["oi"].astype("Int64")
        final["volume"] = final["volume"].astype("Int64")
        final["settle"] = final["settle"].astype("Float64")
        final["impvol"] = final["impvol"].astype("Float64")

        # Never write a file that is smaller than what we already had — that can
        # only mean the fetch came back degraded (entitlement blip, partial
        # session) and would quietly destroy history.
        if PARQUET_PATH.exists():
            prev_rows = len(pd.read_parquet(PARQUET_PATH))
            delta = len(final) - prev_rows
            if delta < 0:
                log.warning("Row count fell by %d (%d -> %d) — expected only if "
                            "contracts expired out of the universe.",
                            -delta, prev_rows, len(final))
            else:
                log.info("Row count %d -> %d (%+d)", prev_rows, len(final), delta)
            if len(final) < prev_rows * 0.9:
                log.error("Refusing to write: %d rows vs %d existing (>10%% shrink). "
                          "Parquet left untouched.", len(final), prev_rows)
                sys.exit(1)

        final.to_parquet(PARQUET_PATH, index=False)

        DASH_DIR.mkdir(parents=True, exist_ok=True)
        atm_data = {}
        if ATM_JSON.exists():
            try:
                atm_data = json.loads(ATM_JSON.read_text())
            except Exception:
                atm_data = {}
        atm_data["KC"] = atm
        atm_data["updated"] = today.strftime("%Y-%m-%d")
        ATM_JSON.write_text(json.dumps(atm_data))

        log.info("Saved -> %s | %d rows | %s -> %s | %d RICs",
                  PARQUET_PATH.name, len(final), final["date"].min().date(), final["date"].max().date(),
                  final["ric"].nunique())
        calls = final[final["option_type"] == "Call"]["ric"].nunique()
        puts  = final[final["option_type"] == "Put"]["ric"].nunique()
        log.info("Calls: %d | Puts: %d | impvol non-null: %d/%d", calls, puts, final["impvol"].notna().sum(), len(final))
        log.info("Strikes: %d distinct, %g - %g", final["strike"].nunique(),
                 final["strike"].min(), final["strike"].max())
        last_oi = final[final["oi"].notna()]
        if len(last_oi):
            d_oi = last_oi["date"].max()
            tot = last_oi[last_oi["date"] == d_oi]["oi"].sum()
            log.info("Latest OI date: %s | total OI across board: %s lots",
                     d_oi.date(), f"{tot:,}")
    finally:
        ld.close_session()

    log.info("=" * 60)


if __name__ == "__main__":
    main()
