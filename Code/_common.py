"""
_common.py — shared plumbing for the per-commodity LSEG options ingest scripts
(cc_ingest_lseg.py, sb_ingest_lseg.py, ct_ingest_lseg.py).

kc_ingest_lseg.py is left untouched (already validated/live) rather than
retrofitted onto this module — no reason to touch a proven script. New
commodities are built on this shared base instead of copy-pasted so the
logic (prefilter, batch fetch, upsert) only exists in one place.
"""

import datetime
import json
import logging
import sys
from pathlib import Path

import pandas as pd
pd.set_option("future.no_silent_downcasting", True)  # silences a harmless lseg.data internal FutureWarning

CALL_CODES = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
PUT_CODES  = {1:"M",2:"N",3:"O",4:"P",5:"Q",6:"R",7:"S",8:"T",9:"U",10:"V",11:"W",12:"X"}
MONTH_NAMES = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
               7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}

FIELDS = ["SETTLE", "OPINT_1", "ACVOL_UNS", "IMP_VOLT"]

today = pd.Timestamp.today().normalize()


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


def get_atm_strike(ld, atm_ric: str, strike_gap: float) -> float:
    """Fetch front-month price and snap to nearest strike_gap increment.
    Stays in real price units throughout (e.g. 362.5 cts/lb, 5900 $/mt) —
    the RIC-encoding multiplier is applied only in build_ric, not here."""
    df = ld.get_data(universe=[atm_ric], fields=["TRDPRC_1"])
    price = float(df["TRDPRC_1"].iloc[0])
    return round(round(price / strike_gap) * strike_gap, 2)


def build_strikes(atm: float, strike_gap: float, strike_steps: int) -> list:
    return [round(atm + i * strike_gap, 2) for i in range(-strike_steps, strike_steps + 1)]


def build_months(months_forward: int) -> list:
    months, m, y = [], today.month, today.year
    for _ in range(months_forward):
        months.append((m, y))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def build_ric(commodity: str, strike: float, month: int, year: int, option_type: str, multiplier: int) -> str:
    code = CALL_CODES[month] if option_type == "Call" else PUT_CODES[month]
    return f"1{commodity}{int(round(strike * multiplier))}{code}{str(year)[-2:]}"


def build_meta(commodity: str, strikes: list, months: list, multiplier: int) -> pd.DataFrame:
    rows = []
    for strike in strikes:
        for (m, y) in months:
            for otype in ("Call", "Put"):
                rows.append({
                    "ric": build_ric(commodity, strike, m, y, otype, multiplier),
                    "option_type": otype,
                    "strike": strike,
                    "expiry_month": m,
                    "expiry_year": y,
                })
    return pd.DataFrame(rows)


def prefilter_live(ld, rics: list, log) -> list:
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
        alive = df[pd.to_numeric(df["OPINT_1"], errors="coerce").fillna(0) > 0]["Instrument"].tolist()
        live.extend(alive)
    return live


def fetch_batch(ld, rics: list, start: str, end: str, log) -> pd.DataFrame:
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


def run_ingest(commodity: str, atm_ric: str, strike_gap: float, strike_steps: int,
                strike_multiplier: int, months_forward: int, backfill_days: int,
                rolling_days: int, batch_size: int,
                parquet_path: Path, atm_json: Path, log, force_full: bool = False):
    """Shared main-loop body. Returns the final DataFrame written to parquet."""
    import lseg.data as ld
    ld.open_session()
    log.info("%s Options Ingest (LSEG) | %s", commodity, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))

    try:
        first_run   = force_full or not parquet_path.exists()
        window_days = backfill_days if first_run else rolling_days
        fetch_start = (today - pd.Timedelta(days=window_days)).strftime("%Y-%m-%d")
        fetch_end   = today.strftime("%Y-%m-%d")
        log.info("Mode: %s | window: %s -> %s", "FULL" if first_run else "INCREMENTAL", fetch_start, fetch_end)

        atm     = get_atm_strike(ld, atm_ric, strike_gap)
        strikes = build_strikes(atm, strike_gap, strike_steps)
        months  = build_months(months_forward)
        meta    = build_meta(commodity, strikes, months, strike_multiplier)
        all_rics = meta["ric"].tolist()

        month_labels = ", ".join(f"{MONTH_NAMES[m]} {y}" for m, y in months)
        log.info("ATM (%s): %s | strikes: %s-%s (%d) | months: %s",
                 atm_ric, atm, strikes[0], strikes[-1], len(strikes), month_labels)
        log.info("Total candidate RICs: %d", len(all_rics))

        live_rics = prefilter_live(ld, all_rics, log)
        log.info("Live (OI>0) RICs: %d / %d", len(live_rics), len(all_rics))

        if not live_rics:
            log.error("No live RICs found — aborting without touching the parquet.")
            sys.exit(1)

        all_dfs = []
        n_batches = (len(live_rics) + batch_size - 1) // batch_size
        for i in range(0, len(live_rics), batch_size):
            batch = live_rics[i:i + batch_size]
            b_num = i // batch_size + 1
            df = fetch_batch(ld, batch, fetch_start, fetch_end, log)
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

        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        window_start = today - pd.Timedelta(days=window_days)

        if first_run:
            final = new_data.drop_duplicates(subset=["date", "ric"], keep="last").sort_values(["ric", "date"]).reset_index(drop=True)
        else:
            existing = pd.read_parquet(parquet_path)
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
        return final
    finally:
        ld.close_session()
