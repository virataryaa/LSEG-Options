"""
CT Options Ingest — LSEG (interim migration)
================================================
LSEG-API replacement for ICEBREAKER/Options/Code/CT_Ingest.py (icepython-based).

RIC scheme confirmed live via discovery.search: 1CT<strike*100><month_code><2-
digit year>, e.g. 1CT8000L26 = CT Dec 2026 80.00 Call. Same *100-cents
encoding as KC/SB, grid step 1 ct/lb (matches ICE's own integer cts/lb strike
step exactly).

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

COMMODITY         = "CT"
ATM_RIC           = "CTc1"
STRIKE_GAP        = 1       # cts/lb
STRIKE_STEPS      = 20      # ATM +/- 20 -> 41 strikes
STRIKE_MULTIPLIER = 100     # cents encoding, same as KC
MONTHS_FORWARD    = 12
BACKFILL_DAYS     = 90
ROLLING_DAYS      = 10
BATCH_SIZE        = 50

PARQUET_PATH = DB_DIR / "CT_options_ice.parquet"
ATM_JSON     = DASH_DIR / "atm.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    log = c.make_logger("ct_ingest_lseg", Path(__file__).parent / "logs")
    c.run_ingest(
        commodity=COMMODITY, atm_ric=ATM_RIC, strike_gap=STRIKE_GAP,
        strike_steps=STRIKE_STEPS, strike_multiplier=STRIKE_MULTIPLIER,
        months_forward=MONTHS_FORWARD, backfill_days=BACKFILL_DAYS,
        rolling_days=ROLLING_DAYS, batch_size=BATCH_SIZE,
        parquet_path=PARQUET_PATH, atm_json=ATM_JSON, log=log,
        force_full=args.full,
    )


if __name__ == "__main__":
    main()
