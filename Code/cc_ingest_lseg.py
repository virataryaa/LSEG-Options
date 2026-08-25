"""
CC Options Ingest — LSEG (interim migration)
================================================
LSEG-API replacement for ICEBREAKER/Options/Code/CC_Ingest.py (icepython-based).

RIC scheme confirmed live via discovery.search: 1CC<strike><month_code><2-digit
year>, e.g. 1CC6100L26 = CC Dec 2026 6100 Call. Unlike KC/SB/CT, the strike is
NOT multiplied by 100 — LSEG's CC option grid is already in whole $/mt
(confirmed against CCc1, which also returns $/mt directly), on a 50-point
grid. This also means, unlike the ICE source, there is no $/mt <-> $/cwt
conversion needed here — ICE's CC options are quoted in $/cwt and its
Ingest.py converts; LSEG's are natively $/mt, matching the futures units and
the Dashboard's expected strike units directly.

Same "no T+1 OI/Volume shift" and "only currently-active contracts have
history" notes as kc_ingest_lseg.py apply here — see that file's docstring
for the full explanation.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as c

DB_DIR   = Path(__file__).parent.parent / "Database"
DASH_DIR = Path(__file__).parent.parent / "Dashboard"

COMMODITY         = "CC"
ATM_RIC           = "CCc1"
STRIKE_GAP        = 50      # $/mt
STRIKE_STEPS      = 40      # ATM +/- 40 -> 81 strikes, +/-2000 $/mt
STRIKE_MULTIPLIER = 1       # CC strikes are whole $/mt already, no *100
MONTHS_FORWARD    = 12
BACKFILL_DAYS     = 90
ROLLING_DAYS      = 10
BATCH_SIZE        = 50

PARQUET_PATH = DB_DIR / "CC_options_ice.parquet"
ATM_JSON     = DASH_DIR / "atm.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Backfill BACKFILL_DAYS instead of incremental")
    parser.add_argument("--days", type=int, default=None, help="Override the fetch window in days")
    parser.add_argument("--legacy-window", action="store_true",
                        help="Use the old ATM +/- window universe instead of LSEG discovery")
    parser.add_argument("--weeklies", action="store_true",
                        help="Also ingest weekly/serial options (collide with monthlies on (strike,month,year))")
    parser.add_argument("--require-oi", action="store_true",
                        help="Prefilter to OI>0 only (old behaviour); default also keeps settle-quoted strikes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report universe and coverage, then exit without writing the parquet")
    args = parser.parse_args()

    log = c.make_logger("cc_ingest_lseg", Path(__file__).parent / "logs")
    c.run_ingest(
        commodity=COMMODITY, atm_ric=ATM_RIC, strike_gap=STRIKE_GAP,
        strike_steps=STRIKE_STEPS, strike_multiplier=STRIKE_MULTIPLIER,
        months_forward=MONTHS_FORWARD, backfill_days=BACKFILL_DAYS,
        rolling_days=ROLLING_DAYS, batch_size=BATCH_SIZE,
        parquet_path=PARQUET_PATH, atm_json=ATM_JSON, log=log,
        force_full=args.full, use_discovery=not args.legacy_window,
        include_weeklies=args.weeklies, require_oi=args.require_oi,
        dry_run=args.dry_run, days=args.days,
    )


if __name__ == "__main__":
    main()
