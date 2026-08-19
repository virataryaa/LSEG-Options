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

Usage:
    python kc_ingest_lseg.py            # incremental (10-day rolling upsert)
    python kc_ingest_lseg.py --full     # backfill BACKFILL_DAYS then upsert
"""

import argparse
import datetime
import json
import logging
import sys
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
STRIKE_STEPS   = 20           # ATM +/- 20 -> 41 strikes
MONTHS_FORWARD = 12
BACKFILL_DAYS  = 90
ROLLING_DAYS   = 10            # matches the ICE source's rolling re-fetch window
BATCH_SIZE     = 50
FIELDS         = ["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"]

CALL_CODES = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
PUT_CODES  = {1:"M",2:"N",3:"O",4:"P",5:"Q",6:"R",7:"S",8:"T",9:"U",10:"V",11:"W",12:"X"}
MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}

today = pd.Timestamp.today().normalize()


# ── Universe construction ────────────────────────────────────────────────────

def get_atm_strike(ld) -> float:
    df = ld.get_data(universe=[ATM_RIC], fields=["TRDPRC_1"])
    price = float(df["TRDPRC_1"].iloc[0])
    return round(round(price / STRIKE_GAP) * STRIKE_GAP, 2)


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
                })
    return pd.DataFrame(rows)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def prefilter_live(ld, rics: list) -> list:
    """Snapshot-check Open Interest in batches, keep only OI > 0 before doing
    the more expensive historical pull — mirrors the ICE source's design."""
    live = []
    for i in range(0, len(rics), 100):
        batch = rics[i:i + 100]
        try:
            df = ld.get_data(universe=batch, fields=["OPINT_1"])
        except Exception as e:
            log.warning("  prefilter batch failed: %s", str(e)[:120])
            continue
        if df is None or df.empty:
            continue
        oi_col = "OPINT_1"
        alive = df[pd.to_numeric(df[oi_col], errors="coerce").fillna(0) > 0]["Instrument"].tolist()
        live.extend(alive)
    return live


def fetch_batch(ld, rics: list, start: str, end: str) -> pd.DataFrame:
    try:
        df = ld.get_history(universe=rics, fields=FIELDS, start=start, end=end, interval="daily")
    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "70005" in err:
            return pd.DataFrame()
        log.warning("  batch error: %s", err[:150])
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

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

    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help=f"Backfill {BACKFILL_DAYS} days instead of incremental")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("KC Options Ingest (LSEG) | %s", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    import lseg.data as ld
    ld.open_session()
    log.info("LSEG session opened.")

    try:
        first_run   = args.full or not PARQUET_PATH.exists()
        window_days = BACKFILL_DAYS if first_run else ROLLING_DAYS
        fetch_start = (today - pd.Timedelta(days=window_days)).strftime("%Y-%m-%d")
        fetch_end   = today.strftime("%Y-%m-%d")
        log.info("Mode: %s | window: %s -> %s", "FULL" if first_run else "INCREMENTAL", fetch_start, fetch_end)

        atm     = get_atm_strike(ld)
        strikes = build_strikes(atm)
        months  = build_months()
        meta    = build_meta(strikes, months)
        all_rics = meta["ric"].tolist()

        month_labels = ", ".join(f"{MONTH_NAMES[m]} {y}" for m, y in months)
        log.info("ATM (%s): %s | strikes: %s-%s (%d) | months: %s",
                 ATM_RIC, atm, strikes[0], strikes[-1], len(strikes), month_labels)
        log.info("Total candidate RICs: %d", len(all_rics))

        live_rics = prefilter_live(ld, all_rics)
        log.info("Live (OI>0) RICs: %d / %d", len(live_rics), len(all_rics))

        if not live_rics:
            log.error("No live RICs found — aborting without touching the parquet.")
            sys.exit(1)

        all_dfs = []
        n_batches = (len(live_rics) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(live_rics), BATCH_SIZE):
            batch = live_rics[i:i + BATCH_SIZE]
            b_num = i // BATCH_SIZE + 1
            df = fetch_batch(ld, batch, fetch_start, fetch_end)
            if not df.empty:
                all_dfs.append(df)
                log.info("  batch %d/%d: %d rows (%d RICs with data)", b_num, n_batches, len(df), df["ric"].nunique())
            else:
                log.info("  batch %d/%d: no data", b_num, n_batches)

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

        if first_run:
            final = new_data.drop_duplicates(subset=["date", "ric"], keep="last").sort_values(["ric", "date"]).reset_index(drop=True)
        else:
            existing = pd.read_parquet(PARQUET_PATH)
            trimmed  = existing[existing["date"] < window_start]
            final = (pd.concat([trimmed, new_data], ignore_index=True)
                      .drop_duplicates(subset=["date", "ric"], keep="last")
                      .sort_values(["ric", "date"]).reset_index(drop=True))

        keep_cols = ["date", "settle", "oi", "volume", "ric", "option_type", "strike", "expiry_month", "expiry_year", "impvol"]
        for c in keep_cols:
            if c not in final.columns:
                final[c] = pd.NA
        final = final[keep_cols]
        final["oi"]     = final["oi"].astype("Int64")
        final["volume"] = final["volume"].astype("Int64")
        final["settle"] = final["settle"].astype("Float64")
        final["impvol"] = final["impvol"].astype("Float64")

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
    finally:
        ld.close_session()

    log.info("=" * 60)


if __name__ == "__main__":
    main()
