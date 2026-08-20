"""
LCC (London Cocoa) Options Ingest — LSEG (interim migration)
================================================
ICE's own LCC_Ingest.py exists but was never run (no parquet in
ICEBREAKER/Options/Database — genuinely orphaned). Its symbol format
("C H26C115000-ICE", GBP/tonne x10) is a different, legacy ICE-feed
convention and does NOT apply to LSEG's own RICs — everything here was
re-confirmed empirically against live LSEG data rather than assumed from
that old script.

- RIC form: <strike><month_code><yy>, e.g. LCC4300I26 = LCC Sep 2026 4300
  Call. No leading "1", same as LRC — LCC is already a distinct root.
- Strike grid: 25-point steps (confirmed via discovery.search: 4300, 4325,
  4375, 4400, 4425... — mostly 25 apart, occasional 50-gaps are just
  strikes with no live OI, not a coarser real grid).
- Active months: London Cocoa trades only Mar/May/Jul/Sep/Dec — 5 months
  a year, NOT the full 12-month cycle. This matches the old (never-run)
  ICE script's own comment about LCC's real-world contract months (H K N
  U Z = Mar/May/Jul/Sep/Dec), independently re-confirmed live here by
  testing all 12 month codes and checking Feb/Apr/Jun/Aug/Oct/Jan/Nov
  genuinely error while Mar/May/Jul/Sep/Dec resolve cleanly.
- ATM field: SETTLE, not TRDPRC_1 — see lrc_ingest_lseg.py's docstring for
  why (off-hours TRDPRC_1 nulls, unrelated to LCC specifically).
- Currency/units: whatever LSEG's own SETTLE/strike scale is (confirmed
  self-consistent: LCCc1 SETTLE and live option strikes are on the same
  numeric scale, ~4268 ATM vs strikes in the high-1000s to mid-5000s) —
  no unit conversion applied, unlike the old ICE script's GBP/tonme x10
  encoding which is specific to that legacy feed, not LSEG.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as c

DB_DIR   = Path(__file__).parent.parent / "Database"
DASH_DIR = Path(__file__).parent.parent / "Dashboard"

COMMODITY         = "LCC"
ATM_RIC           = "LCCc1"
ATM_FIELD         = "SETTLE"
STRIKE_GAP        = 25
STRIKE_STEPS      = 40      # ATM +/- 40 -> 81 strikes, +/-1000
STRIKE_MULTIPLIER = 1       # raw scale, no *100 encoding (confirmed live)
RIC_PREFIX        = ""      # no "1" disambiguator (confirmed live)
ALLOWED_MONTHS    = {3, 5, 7, 9, 12}   # Mar/May/Jul/Sep/Dec only
MONTHS_FORWARD    = 6       # ~5 expiries/year -> a bit over a year forward
BACKFILL_DAYS     = 90
ROLLING_DAYS      = 10
BATCH_SIZE        = 50

PARQUET_PATH = DB_DIR / "LCC_options_ice.parquet"
ATM_JSON     = DASH_DIR / "atm.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()

    log = c.make_logger("lcc_ingest_lseg", Path(__file__).parent / "logs")
    c.run_ingest(
        commodity=COMMODITY, atm_ric=ATM_RIC, strike_gap=STRIKE_GAP,
        strike_steps=STRIKE_STEPS, strike_multiplier=STRIKE_MULTIPLIER,
        months_forward=MONTHS_FORWARD, backfill_days=BACKFILL_DAYS,
        rolling_days=ROLLING_DAYS, batch_size=BATCH_SIZE,
        parquet_path=PARQUET_PATH, atm_json=ATM_JSON, log=log,
        force_full=args.full, ric_prefix=RIC_PREFIX,
        allowed_months=ALLOWED_MONTHS, atm_field=ATM_FIELD,
    )


if __name__ == "__main__":
    main()
