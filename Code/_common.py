"""
_common.py — shared plumbing for the per-commodity LSEG options ingest scripts
(cc_ingest_lseg.py, sb_ingest_lseg.py, ct_ingest_lseg.py, lrc_, lcc_).

kc_ingest_lseg.py stands alone (it was validated first and carries the same
logic inline); everything else is built on this shared base so the logic
(discovery, prefilter, batch fetch, upsert) only exists in one place.

UNIVERSE — discovery vs the old ATM window
------------------------------------------
The universe used to be *guessed*: ATM +/- strike_steps * strike_gap x N
forward months. Measured on KC against LSEG's actual listed board
(2026-08-25) that captured 159 of 1,730 live monthly RICs and only 27% of
open interest — 148,725 lots sat on strikes outside the window versus 54,940
inside it, overwhelmingly deep OTM producer puts. The same shape of blind
spot applies to every commodity built from an arithmetic window.

The universe is now DISCOVERED from LSEG search (DERIVATIVE_QUOTES view,
startswith(RIC, <prefix><COMMODITY>)), which returns every listed option with
its strike, put/call flag and expiry. `--legacy-window` falls back to the old
arithmetic window, and discovery failure falls back automatically.

WEEKLIES ARE DELIBERATELY EXCLUDED. LSEG also lists weekly/serial options
under a different RIC shape — <prefix><COMMODITY><n>W<strike><code><single
digit year> — alongside the monthly <prefix><COMMODITY><strike><code><yy>.
Both collapse to the same (strike, expiry_month, expiry_year) key that these
parquets and the dashboard pivot on, so ingesting them would let a weekly
silently overwrite a monthly under pivot_table(aggfunc="first"). Pass
--weeklies to include them anyway; the `series` column records provenance.
"""

import datetime
import json
import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd
pd.set_option("future.no_silent_downcasting", True)  # silences a harmless lseg.data internal FutureWarning

CALL_CODES = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
PUT_CODES  = {1:"M",2:"N",3:"O",4:"P",5:"Q",6:"R",7:"S",8:"T",9:"U",10:"V",11:"W",12:"X"}
MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}

FIELDS = ["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"]

CODE_TO_CALL_MONTH = {v: k for k, v in CALL_CODES.items()}
CODE_TO_PUT_MONTH  = {v: k for k, v in PUT_CODES.items()}

SEARCH_TOP     = 10000
PREFILTER_SIZE = 100
FETCH_RETRIES  = 3   # transient LSEG timeouts/429s are common on wide universes
FETCH_BACKOFF  = 5   # seconds, doubled per retry
ACTIVE_LOOKBACK = 10 # days of OI/volume history that make a RIC "active" (--active-only)

# Secondary guard so a RIC prefix cannot drag in an unrelated instrument
# (e.g. startswith '1CC' also matches non-cocoa tickers).
NAME_FILTER = {"CC": "Cocoa", "LCC": "Cocoa", "SB": "Sugar",
               "CT": "Cotton", "KC": "Coffee", "LRC": "Coffee"}

today = pd.Timestamp.today().normalize()


def _ric_patterns(commodity: str, prefix: str = "1"):
    """(monthly, weekly) regexes for a commodity's option RICs."""
    root = re.escape(f"{prefix}{commodity}")
    return (re.compile(rf"^{root}(\d+)([A-X])(\d{{2}})$"),
            re.compile(rf"^{root}(\d)W(\d+)([A-X])(\d)$"))


def parse_ric(ric: str, commodity: str, multiplier: int, prefix: str = "1"):
    """Decode an LSEG option RIC into its contract fields, or None if it is not
    one (the search prefix can also match unrelated instruments).

    The month code is the CONTRACT month, not the expiry date: LSEG lists
    1KC25000X26 as '... 250 Put Dec 2026' with ExpiryDate 2026-11-12, because
    these options expire the month before the futures month. The parquets and
    the dashboard key on the contract month, so that is what is stored.
    """
    monthly_re, weekly_re = _ric_patterns(commodity, prefix)
    m = monthly_re.match(ric)
    if m:
        raw, code, yy = m.group(1), m.group(2), m.group(3)
        series, year = "monthly", 2000 + int(yy)
    else:
        w = weekly_re.match(ric)
        if not w:
            return None
        raw, code, y1 = w.group(2), w.group(3), w.group(4)
        base = today.year - (today.year % 10)      # single-digit year
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

    return {"ric": ric, "option_type": otype,
            "strike": int(raw) / float(multiplier),
            "expiry_month": month, "expiry_year": year, "series": series}


def discover_meta(ld, commodity: str, multiplier: int, log, prefix: str = "1",
                  include_weeklies: bool = False, exchange_code: str = None):
    """Enumerate the live listed option board from LSEG search.

    Returns the same meta columns as build_meta() so the rest of the pipeline
    is unchanged, plus a `series` column.

    exchange_code: server-side ExchangeCode filter (e.g. 'IEU' for ICE Europe).
    Needed for LRC/LCC, which skip the '1' disambiguator prefix (already an
    unambiguous 3-letter root on their own exchange) — but LSEG's RIC-prefix
    search has no exchange scoping by default, so 'startswith(RIC,"LRC")' also
    matches unrelated OPRA equity options on tickers that happen to start with
    the same letters (confirmed live 2026-09-17: Lam Research Corp equity
    options under "LRC*", and unrelated non-coffee ICE Europe instruments under
    "LRC*" too). Both LRC and LCC's true match count (12,357 / 13,980) exceeds
    LSEG's 10,000-row search cap, so without this filter the truncation could
    silently drop real contracts before the DTSubjectName name-filter below
    ever runs. Filtering to ExchangeCode='IEU' cut LRC to 3,207 and LCC to
    6,603 — both safely under the cap — while the name filter below still
    cleans up the small remaining same-exchange noise (e.g. other tickers'
    Flex Equity options also listed on ICE Europe). KC/CC/SB/CT keep their '1'
    prefix and were confirmed NOT to hit the cap (KC: 2,127 total, all genuine
    ICE US/CBT/IOM) — exchange_code stays None for those, unchanged.
    """
    from lseg.data.content import search

    search_prefix = f"{prefix}{commodity}"
    filt = f"startswith(RIC,'{search_prefix}') and ExpiryDate ne null"
    if exchange_code:
        filt += f" and ExchangeCode eq '{exchange_code}'"
    r = search.Definition(
        view=search.Views.DERIVATIVE_QUOTES,
        filter=filt,
        select="RIC,DTSubjectName,ExpiryDate,StrikePrice,CallPutOption",
        top=SEARCH_TOP,
    ).get_data()
    d = r.data.df
    if d is None or d.empty:
        raise RuntimeError(f"search returned no rows for {search_prefix}")
    if len(d) >= SEARCH_TOP:
        log.warning("search hit the %d row cap — universe may be truncated", SEARCH_TOP)

    d = d.copy()
    d["ExpiryDate"] = pd.to_datetime(d["ExpiryDate"], errors="coerce")
    d["StrikePrice"] = pd.to_numeric(d["StrikePrice"], errors="coerce")
    name_re = NAME_FILTER.get(commodity)
    if name_re:
        keep = d["DTSubjectName"].astype(str).str.contains(name_re, case=False, na=False)
        log.info("Search: %d rows, %d after '%s' name filter", len(d), int(keep.sum()), name_re)
        d = d[keep]

    live = d[(d["ExpiryDate"] > today) & (d["StrikePrice"] > 0)]
    log.info("Search: %d live (unexpired, strike>0)", len(live))

    parsed = [p for p in (parse_ric(x, commodity, multiplier, prefix)
                          for x in live["RIC"].unique()) if p]
    meta = pd.DataFrame(parsed)
    if meta.empty:
        raise RuntimeError(f"no {commodity} RICs parsed from search results")

    n_week = int((meta["series"] != "monthly").sum())
    if include_weeklies:
        log.info("Universe: %d monthly + %d weekly/serial (weeklies INCLUDED)",
                 len(meta) - n_week, n_week)
    else:
        meta = meta[meta["series"] == "monthly"].reset_index(drop=True)
        log.info("Universe: %d monthly (excluded %d weekly/serial — they collide "
                 "with monthlies on (strike, month, year))", len(meta), n_week)

    # cross-check what we decoded from the RIC against the search metadata
    chk = meta.merge(live[["RIC", "StrikePrice", "CallPutOption"]].drop_duplicates("RIC"),
                     left_on="ric", right_on="RIC", how="left")
    bad = chk[chk["StrikePrice"].notna() &
              ((chk["StrikePrice"] - chk["strike"]).abs() > 1e-6)]
    bad_cp = chk[chk["CallPutOption"].notna() & (chk["CallPutOption"] != chk["option_type"])]
    if len(bad):
        log.warning("%d RICs where decoded strike != search strike, e.g. %s", len(bad),
                    bad[["ric", "strike", "StrikePrice"]].head(3).to_dict("records"))
    if len(bad_cp):
        log.warning("%d RICs where decoded call/put != search, e.g. %s", len(bad_cp),
                    bad_cp[["ric", "option_type", "CallPutOption"]].head(3).to_dict("records"))
    if not len(bad) and not len(bad_cp):
        log.info("RIC decode cross-checked against search metadata: all %d agree", len(chk))

    return meta[["ric", "option_type", "strike", "expiry_month", "expiry_year", "series"]]


def make_logger(name: str, log_dir: Path):
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # don't let messages bubble to root, and don't catch root's (e.g. httpx's)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt)
    fh = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8"); fh.setFormatter(fmt)
    logger.addHandler(sh); logger.addHandler(fh)
    # lseg.data logs each HTTP request at INFO via the "httpx" logger; left
    # unsuppressed this can flood the automator's status email (seen on KC,
    # which previously used logging.basicConfig and leaked this onto root).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logger


def get_atm_strike(ld, atm_ric: str, strike_gap: float, atm_field: str = "TRDPRC_1") -> float:
    """Fetch front-month price and snap to nearest strike_gap increment.
    Stays in real price units throughout (e.g. 362.5 cts/lb, 5900 $/mt) —
    the RIC-encoding multiplier is applied only in build_ric, not here.
    atm_field defaults to TRDPRC_1 (last trade) to match the already-proven
    KC/CC/SB/CT scripts unchanged; SETTLE is used for LRC/LCC since
    TRDPRC_1 was observed null off-hours while SETTLE is always populated.

    TRDPRC_1 can also go null off-hours for KC/CC/SB/CT (seen live on CC,
    2026-08-25 — a run that otherwise succeeded for KC/SB/CT the same
    morning), so falls back to SETTLE rather than crashing when that
    happens, instead of only ever relying on the caller's chosen field.

    This is also the first call of a run, so it RETRIES: an unretried timeout
    here aborts the whole ingest before a single row is fetched (observed on
    KC, 2026-08-26, when the local Workspace proxy was briefly unresponsive).
    """
    fields = [atm_field] if atm_field == "SETTLE" else [atm_field, "SETTLE"]
    delay, last = FETCH_BACKOFF, None
    for attempt in range(1, FETCH_RETRIES + 1):
        for field in fields:
            try:
                df = ld.get_data(universe=[atm_ric], fields=[field])
                price = df[field].iloc[0]
                if not pd.isna(price):
                    return round(round(float(price) / strike_gap) * strike_gap, 2)
            except Exception as e:
                last = e
        if attempt < FETCH_RETRIES:
            time.sleep(delay)
            delay *= 2
    raise ValueError(f"{atm_ric}: no ATM price from {fields} after {FETCH_RETRIES} attempts "
                     f"({str(last)[:120] if last else 'all null'})")


def build_strikes(atm: float, strike_gap: float, strike_steps: int) -> list:
    return [round(atm + i * strike_gap, 2) for i in range(-strike_steps, strike_steps + 1)]


def build_months(months_forward: int, allowed_months: set = None) -> list:
    """Next `months_forward` listed expiries from today. allowed_months restricts
    to a subset of calendar months (e.g. LRC trades Jan/Mar/May/Jul/Sep/Nov only,
    LCC trades Mar/May/Jul/Sep/Dec only) — confirmed empirically per-commodity,
    not a general assumption. Without it, every calendar month is listed (KC/CC/SB/CT)."""
    months, m, y = [], today.month, today.year
    while len(months) < months_forward:
        if allowed_months is None or m in allowed_months:
            months.append((m, y))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def build_ric(commodity: str, strike: float, month: int, year: int, option_type: str,
              multiplier: int, prefix: str = "1") -> str:
    code = CALL_CODES[month] if option_type == "Call" else PUT_CODES[month]
    return f"{prefix}{commodity}{int(round(strike * multiplier))}{code}{str(year)[-2:]}"


def build_meta(commodity: str, strikes: list, months: list, multiplier: int, prefix: str = "1") -> pd.DataFrame:
    rows = []
    for strike in strikes:
        for (m, y) in months:
            for otype in ("Call", "Put"):
                rows.append({
                    "ric": build_ric(commodity, strike, m, y, otype, multiplier, prefix),
                    "option_type": otype,
                    "strike": strike,
                    "expiry_month": m,
                    "expiry_year": y,
                })
    return pd.DataFrame(rows)


def prefilter_live(ld, rics: list, log, require_oi: bool = False) -> list:
    """Snapshot-check the board in batches, keeping only instruments that are
    actually quoted, before the more expensive historical pull.

    The original kept OI > 0 only. That is too tight once the universe is
    discovered rather than guessed: on KC, 1,722 of 1,730 live monthlies
    carried a settle but only 484 carried OI, and the settle-only contracts
    are exactly the wings the vol surface and Px Change panels need. Keeping
    "OI > 0 OR settle present" preserves those without pulling history for
    genuinely dead RICs. require_oi=True restores the old behaviour.
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
    log.info("  prefilter: %d with OI>0, %d with settle -> %d kept", seen_oi, seen_px, len(live))
    return live


def fetch_batch(ld, rics: list, start: str, end: str, log, retries: int = FETCH_RETRIES):
    """Fetch a batch of RICs. Returns (dataframe, definitive).

    `definitive` distinguishes "LSEG says there is no such data" from "the call
    failed". That matters because the incremental upsert drops the existing rows
    inside the refresh window before writing the new ones — so a transient
    timeout or 429 treated as an empty result silently deletes history. A
    measured KC incremental run lost 600 rows to exactly that. Failed batches
    are retried with backoff, and any that still fail are reported so the caller
    can protect their existing rows.
    """
    delay = FETCH_BACKOFF
    for attempt in range(1, retries + 1):
        try:
            df = ld.get_history(universe=rics, fields=FIELDS, start=start, end=end, interval="daily")
        except Exception as e:
            err = str(e)
            if "not found" in err.lower() or "70005" in err:
                return pd.DataFrame(), True       # genuinely absent
            if attempt < retries:
                # A 429 means the proxy is throttling the whole run, not just
                # this batch — the generic 5s/10s backoff was measured too
                # short on 2026-09-15 (CT lost 318 RICs, LRC/LCC failed outright
                # to sustained 429s that never cleared within 3 retries). Same
                # fix as kc_ingest_lseg.py: rate-limit errors get a longer,
                # steeper wait so later batches have a real chance to land.
                is_429 = "429" in err or "too many requests" in err.lower()
                wait = max(delay, 30) if is_429 else delay
                log.warning("  batch error (attempt %d/%d)%s: %s — retrying in %ds",
                            attempt, retries, " [rate-limited]" if is_429 else "", err[:110], wait)
                time.sleep(wait)
                delay = wait * 2
                continue
            log.error("  batch FAILED after %d attempts: %s", retries, err[:150])
            return pd.DataFrame(), False          # could not be established
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


def topup_open_interest(ld, parquet_path: Path, log) -> int:
    """Fill the last completed session's OI from the real-time quote.

    Same technique as Futures/Code/futures_builder_lseg.py, ported to options
    via kc_ingest_lseg.py (verified live for KC on 2026-09-16: snapshot OPINT_1
    matched the last stored session's OI on every comparable RIC). get_history's
    daily file does not carry the newest session's OPINT_1 until after
    midnight; the real-time quote for the same RIC already does — one session
    behind, never today's intraday tally.

    Freshness is judged market-wide, not per-contract: a quiet strike can
    carry an unchanged OI for days for real, so "unchanged" alone never proves
    the exchange hasn't published yet. Only accept the batch if a majority of
    comparable RICs actually moved.
    """
    if not parquet_path.exists():
        return 0
    df = pd.read_parquet(parquet_path)
    df["date"] = pd.to_datetime(df["date"])

    prior = df[df["date"] < today]
    if prior.empty:
        return 0
    target = prior["date"].max()

    need = df[(df["date"] == target) & df["settle"].notna() & df["oi"].isna()]
    if need.empty:
        log.info("OI already complete for %s", target.date())
        return 0

    rics = sorted(need["ric"].unique())
    snaps = []
    for i in range(0, len(rics), PREFILTER_SIZE):
        batch = rics[i:i + PREFILTER_SIZE]
        try:
            snaps.append(ld.get_data(universe=batch, fields=["OPINT_1"]))
        except Exception as e:
            log.warning("  OI top-up quote batch failed: %s", str(e)[:120])
    if not snaps:
        return 0
    snap = pd.concat(snaps, ignore_index=True).set_index("Instrument")["OPINT_1"]

    prev_oi = (df[(df["date"] < target) & df["oi"].notna()]
               .sort_values("date").groupby("ric")["oi"].last())

    comparable = [r for r in rics if pd.notna(prev_oi.get(r)) and pd.notna(snap.get(r))]
    if not comparable:
        log.info("OI top-up: no RIC with a prior OI to check the quote against — "
                 "skipped, left for the historical fetch")
        return 0
    moved = [r for r in comparable if abs(float(snap[r]) - float(prev_oi[r])) >= 1]
    if len(moved) * 2 < len(comparable):
        log.info("OI top-up: only %d/%d comparable RICs show a changed OI — "
                 "%s not published yet, left for the next run", len(moved), len(comparable), target.date())
        return 0

    filled = 0
    for ric in rics:
        v = snap.get(ric)
        if pd.isna(v):
            continue
        df.loc[(df["date"] == target) & (df["ric"] == ric), "oi"] = float(v)
        filled += 1
    if filled:
        df["oi"] = df["oi"].astype("Int64")
        df = df.sort_values(["ric", "date"]).reset_index(drop=True)
        df.to_parquet(parquet_path, index=False)
        log.info("OI top-up: filled OI on %s for %d contract(s) (%d/%d comparable moved)",
                 target.date(), filled, len(moved), len(comparable))
    return filled


def run_ingest(commodity: str, atm_ric: str, strike_gap: float, strike_steps: int,
                strike_multiplier: int, months_forward: int, backfill_days: int,
                rolling_days: int, batch_size: int,
                parquet_path: Path, atm_json: Path, log, force_full: bool = False,
                ric_prefix: str = "1", allowed_months: set = None, atm_field: str = "TRDPRC_1",
                use_discovery: bool = True, include_weeklies: bool = False,
                require_oi: bool = False, dry_run: bool = False, days: int = None,
                no_topup: bool = False, exchange_code: str = None, active_only: bool = False,
                max_expiries: int = None, max_strike_pct: float = None, max_strikes: int = None,
                strike_skip: int = 0):
    """Shared main-loop body. Returns the final DataFrame written to parquet.
    ric_prefix: '1' for KC/CC/SB/CT-style RICs, '' for LRC/LCC (root is already
    unambiguous, no disambiguator prefix — confirmed live via discovery.search).
    allowed_months: restricts to a commodity's actual listed contract months
    (e.g. LRC = Jan/Mar/May/Jul/Sep/Nov, LCC = Mar/May/Jul/Sep/Dec) instead of
    every calendar month — also confirmed live, not assumed."""
    import lseg.data as ld
    ld.open_session()
    log.info("%s Options Ingest (LSEG) | %s", commodity, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    try:
        first_run   = force_full or not parquet_path.exists()
        window_days = days if days else (backfill_days if first_run else rolling_days)
        fetch_start = (today - pd.Timedelta(days=window_days)).strftime("%Y-%m-%d")
        fetch_end   = today.strftime("%Y-%m-%d")
        log.info("Mode: %s | window: %s -> %s (%dd)",
                 "FULL" if first_run else "INCREMENTAL", fetch_start, fetch_end, window_days)

        atm = get_atm_strike(ld, atm_ric, strike_gap, atm_field)

        meta = None
        if use_discovery:
            try:
                meta = discover_meta(ld, commodity, strike_multiplier, log,
                                     prefix=ric_prefix, include_weeklies=include_weeklies,
                                     exchange_code=exchange_code)
            except Exception as e:
                log.warning("Discovery failed (%s) — falling back to the ATM window.", str(e)[:160])
                meta = None
        if meta is None:
            strikes = build_strikes(atm, strike_gap, strike_steps)
            months  = build_months(months_forward, allowed_months)
            meta    = build_meta(commodity, strikes, months, strike_multiplier, ric_prefix)
            log.info("ATM (%s): %s | legacy window strikes %s-%s (%d) x %d months",
                     atm_ric, atm, strikes[0], strikes[-1], len(strikes), len(months))

        if max_expiries:
            # Applied BEFORE prefilter/fetch, not just at display time — a
            # commodity that genuinely lists 2-3 years forward (Cotton: 24
            # distinct expiries out to mid-2029 vs KC's 9) pays the full
            # per-RIC rate-limit cost for every one of those far-dated
            # expiries unless trimmed here, even though nobody's looking
            # at a 3-year-out serial strike day to day.
            uniq_exp = (meta[["expiry_year", "expiry_month"]].drop_duplicates()
                        .sort_values(["expiry_year", "expiry_month"]))
            keep_exp = set(map(tuple, uniq_exp.head(max_expiries).to_numpy()))
            before = len(meta)
            meta = meta[meta.apply(lambda r: (r["expiry_year"], r["expiry_month"]) in keep_exp, axis=1)].reset_index(drop=True)
            if len(uniq_exp) > max_expiries:
                log.info("MAX-EXPIRIES: kept nearest %d of %d listed expiries (%d -> %d candidate RICs)",
                         max_expiries, len(uniq_exp), before, len(meta))

        if max_strike_pct:
            # Same idea as max_expiries, for the strike axis. Checked live
            # 2026-09-21: CC and LCC both list strikes 3x+ the current price
            # away from ATM, but strikes within +/-100% of ATM already
            # capture 95%+ of total open interest for every commodity — the
            # far wings cost real RICs (CC: 237->164, cutting 73 for <5% of
            # OI) for strikes nobody is actually positioned in.
            lo, hi = atm * (1 - max_strike_pct / 100), atm * (1 + max_strike_pct / 100)
            before = len(meta)
            n_strikes_before = meta["strike"].nunique()
            meta = meta[(meta["strike"] >= lo) & (meta["strike"] <= hi)].reset_index(drop=True)
            if meta["strike"].nunique() < n_strikes_before:
                log.info("MAX-STRIKE-PCT: kept strikes within +/-%.0f%% of ATM (%s-%s) — "
                         "%d -> %d distinct strikes, %d -> %d candidate RICs",
                         max_strike_pct, round(lo, 4), round(hi, 4),
                         n_strikes_before, meta["strike"].nunique(), before, len(meta))

        if max_strikes:
            # Hard cap on strike COUNT, centered on ATM — distinct from
            # max_strike_pct (a percentage band): this keeps exactly the N
            # strikes nearest to the current price regardless of how the
            # board is shaped, so every commodity has a known, comparable
            # ceiling on RICs-per-expiry no matter how tightly or widely its
            # strikes are spaced.
            #
            # strike_skip lets a later, separate run fetch the NEXT band
            # outward instead of the same innermost strikes again — e.g.
            # strike_skip=100, max_strikes=50 fetches ranks 101-150 by
            # distance from ATM, so a staged "100 now, +50 later" rollout
            # only ever asks LSEG for the strikes it doesn't already have,
            # rather than re-fetching the inner 100 every time.
            uniq_strikes = meta["strike"].drop_duplicates()
            n_strikes_before = len(uniq_strikes)
            ranked = uniq_strikes.iloc[(uniq_strikes - atm).abs().argsort()]
            band = ranked.iloc[strike_skip:strike_skip + max_strikes]
            if len(band) < n_strikes_before or strike_skip:
                keep_strikes = set(band)
                before = len(meta)
                meta = meta[meta["strike"].isin(keep_strikes)].reset_index(drop=True)
                if strike_skip:
                    log.info("MAX-STRIKES: skipped nearest %d, kept the next %d strikes to ATM "
                             "(ranks %d-%d of %d, ATM=%s) — %d -> %d candidate RICs",
                             strike_skip, len(band), strike_skip + 1, strike_skip + len(band),
                             n_strikes_before, atm, before, len(meta))
                else:
                    log.info("MAX-STRIKES: kept nearest %d of %d strikes to ATM (%s) — %d -> %d candidate RICs",
                             max_strikes, n_strikes_before, atm, before, len(meta))

        all_rics = meta["ric"].tolist()
        log.info("ATM (%s): %s | candidate RICs: %d | strikes %g-%g (%d distinct) | expiries: %d",
                 atm_ric, atm, len(all_rics), meta["strike"].min(), meta["strike"].max(),
                 meta["strike"].nunique(), meta.groupby(["expiry_year", "expiry_month"]).ngroups)

        t0 = time.time()
        live_rics = prefilter_live(ld, all_rics, log, require_oi=require_oi)
        log.info("Quoted RICs: %d / %d (%.0fs)", len(live_rics), len(all_rics), time.time() - t0)

        # Ported from kc_ingest_lseg.py, same logic: wall time is linear in RIC
        # count, so the only way to shrink a daily run is to fetch fewer RICs.
        # Keep only RICs that traded or held OI recently; the rest are
        # settle-only wings that carry no positioning and can refresh on
        # whatever cadence a full (non-active-only) sweep runs instead.
        skipped_rics = []
        if active_only and parquet_path.exists():
            prev = pd.read_parquet(parquet_path)
            prev["date"] = pd.to_datetime(prev["date"])
            recent = prev[prev["date"] >= prev["date"].max() - pd.Timedelta(days=ACTIVE_LOOKBACK)]
            active = set(recent[pd.to_numeric(recent["oi"], errors="coerce") > 0]["ric"]) | \
                     set(recent[pd.to_numeric(recent["volume"], errors="coerce") > 0]["ric"])
            newly = set(live_rics) - set(prev["ric"])  # never-seen RICs have no history to judge by
            trimmed = [r for r in live_rics if r in active or r in newly]
            if trimmed:
                log.info("ACTIVE-ONLY: %d of %d RICs traded or held OI in the last %dd (+%d newly listed)",
                         len(trimmed), len(live_rics), ACTIVE_LOOKBACK, len(newly))
                # RICs we choose NOT to fetch must be protected exactly like a
                # failed batch: the incremental upsert clears the refresh
                # window before writing, so without this their recent rows
                # would be deleted rather than left alone.
                skipped_rics = [r for r in live_rics if r not in set(trimmed)]
                live_rics = trimmed
            else:
                log.warning("ACTIVE-ONLY matched nothing — falling back to the full quoted set.")

        if not live_rics:
            log.error("No live RICs found — aborting without touching the parquet.")
            sys.exit(1)

        if dry_run:
            lm = meta[meta["ric"].isin(live_rics)]
            log.info("DRY RUN — would fetch %d RICs, strikes %g-%g (%d distinct)",
                     len(live_rics), lm["strike"].min(), lm["strike"].max(), lm["strike"].nunique())
            if parquet_path.exists():
                cur = pd.read_parquet(parquet_path)
                log.info("Current parquet: %d RICs, %d strikes (%g-%g)",
                         cur["ric"].nunique(), cur["strike"].nunique(),
                         cur["strike"].min(), cur["strike"].max())
                gain = sorted(set(lm["strike"]) - set(cur["strike"]))
                log.info("Strikes that would be ADDED (%d): %s", len(gain), gain)
            log.info("DRY RUN — nothing written.")
            return None

        all_dfs, failed_rics, partial_rics = [], [], []
        n_batches = (len(live_rics) + batch_size - 1) // batch_size
        t0 = time.time()
        for i in range(0, len(live_rics), batch_size):
            batch = live_rics[i:i + batch_size]
            b_num = i // batch_size + 1
            df, definitive = fetch_batch(ld, batch, fetch_start, fetch_end, log)
            if not df.empty:
                all_dfs.append(df)
                # A batch can succeed while silently returning fewer RICs than
                # asked for. Those RICs are not "failed", so without this they
                # fall through to the upsert, which clears the refresh window
                # and has nothing to write back — deleting their recent
                # history (same fix as kc_ingest_lseg.py; observed on CT/SB
                # 2026-09-15, where lost rows had to be re-backfilled later).
                missing = [r for r in batch if r not in set(df["ric"].unique())]
                if missing:
                    partial_rics.extend(missing)
                log.info("  batch %d/%d: %d rows (%d/%d RICs with data%s)", b_num, n_batches,
                         len(df), df["ric"].nunique(), len(batch),
                         f", {len(missing)} not returned — preserved" if missing else "")
            elif definitive:
                log.info("  batch %d/%d: no data", b_num, n_batches)
            else:
                failed_rics.extend(batch)
                log.warning("  batch %d/%d: UNRESOLVED — existing rows for these %d RICs will be preserved",
                            b_num, n_batches, len(batch))
        log.info("History fetch complete in %.0fs", time.time() - t0)
        if failed_rics:
            log.warning("%d RICs could not be fetched this run; their history is kept as-is.",
                        len(failed_rics))
        if partial_rics:
            log.warning("%d RICs were requested but not returned by an otherwise-successful batch; "
                        "their existing rows are preserved rather than cleared.", len(partial_rics))

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

        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        window_start = today - pd.Timedelta(days=window_days)

        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            if "series" not in existing.columns:
                existing["series"] = "monthly"   # pre-discovery data was monthly by construction
            existing["date"] = pd.to_datetime(existing["date"])
            if first_run:
                # A backfill must never lose history: keep every existing row and
                # let the freshly fetched window override it on (date, ric).
                # Replacing outright silently drops the days that sit before the
                # backfill window.
                base = existing
            else:
                # Keep rows outside the refresh window, plus every row belonging
                # to a RIC whose batch could not be fetched — otherwise a
                # transient failure deletes that RIC's window instead of
                # leaving it alone.
                keep = existing["date"] < window_start
                protect = set(failed_rics) | set(skipped_rics) | set(partial_rics)
                if protect:
                    keep = keep | existing["ric"].isin(protect)
                base = existing[keep]
            final = (pd.concat([base, new_data], ignore_index=True)
                      .drop_duplicates(subset=["date", "ric"], keep="last")
                      .sort_values(["ric", "date"]).reset_index(drop=True))
        else:
            final = (new_data.drop_duplicates(subset=["date", "ric"], keep="last")
                     .sort_values(["ric", "date"]).reset_index(drop=True))

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

        # Never write a file materially smaller than what we already had — that
        # can only mean a degraded fetch, and would quietly destroy history.
        if parquet_path.exists():
            prev_rows = len(pd.read_parquet(parquet_path))
            delta = len(final) - prev_rows
            if delta < 0:
                log.warning("Row count fell by %d (%d -> %d) — expected only if contracts "
                            "expired out of the universe.", -delta, prev_rows, len(final))
            else:
                log.info("Row count %d -> %d (%+d)", prev_rows, len(final), delta)
            if len(final) < prev_rows * 0.9:
                log.error("Refusing to write: %d rows vs %d existing (>10%% shrink). "
                          "Parquet left untouched.", len(final), prev_rows)
                sys.exit(1)

        final.to_parquet(parquet_path, index=False)

        atm_json.parent.mkdir(parents=True, exist_ok=True)
        atm_data = {}
        if atm_json.exists():
            try:
                atm_data = json.loads(atm_json.read_text())
            except Exception:
                atm_data = {}
        atm_data[commodity] = atm
        atm_data["updated"] = today.strftime("%Y-%m-%d")
        atm_json.write_text(json.dumps(atm_data))

        log.info("Saved -> %s | %d rows | %s -> %s | %d RICs",
                  parquet_path.name, len(final), final["date"].min().date(), final["date"].max().date(),
                  final["ric"].nunique())
        calls = final[final["option_type"] == "Call"]["ric"].nunique()
        puts  = final[final["option_type"] == "Put"]["ric"].nunique()
        log.info("Calls: %d | Puts: %d | impvol non-null: %d/%d", calls, puts, final["impvol"].notna().sum(), len(final))
        log.info("Strikes: %d distinct, %g - %g", final["strike"].nunique(),
                 final["strike"].min(), final["strike"].max())
        last_oi = final[final["oi"].notna()]
        if len(last_oi):
            d_oi = last_oi["date"].max()
            log.info("Latest OI date: %s | total OI across board: %s lots",
                     d_oi.date(), f"{last_oi[last_oi['date'] == d_oi]['oi'].sum():,}")

        if not no_topup:
            filled = topup_open_interest(ld, parquet_path, log)
            if filled:
                log.info("OI top-up: %d contract(s) filled for the prior session", filled)
        return final
    finally:
        ld.close_session()
