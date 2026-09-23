"""
LRC (Robusta Coffee) Options Ingest — LSEG (interim migration)
================================================
No ICE source to port from — LRC options were never built on the ICE side
of this project. RIC scheme, strike grid, and active-month cycle were all
confirmed empirically against live LSEG data before building this script:

- RIC form: <strike><month_code><yy>, e.g. LRC3700I26 = LRC Sep 2026 3700
  Call. Unlike KC/CC/SB/CT there is NO leading "1" — LRC is already a
  distinct root with no disambiguation needed (confirmed via
  discovery.search: real listed titles are "ICE Robusta Coffee Commodity
  Option 3700 Call Sep 2026" etc., resolving cleanly as bare "LRC3700I26").
- Strike grid: 25-point steps, matching CC's un-multiplied raw-price
  encoding (confirmed via discovery.search sampling strikes around ATM:
  3575, 3600, 3625, 3650... all 25 apart).
- Active months: Robusta trades only Jan/Mar/May/Jul/Sep/Nov (odd months),
  NOT the full 12-month cycle KC/CC/SB/CT use. Confirmed by testing all 12
  month codes live: even months (Feb/Apr/Jun/Aug/Oct/Dec) genuinely error
  ("record could not be found"), odd months resolve cleanly — verified
  against a known-fake RIC to confirm get_data actually distinguishes
  valid from invalid instruments rather than silently returning null for
  both.
- ATM field: SETTLE, not TRDPRC_1 — TRDPRC_1 (last trade) was observed
  null off-hours for LRCc1 (and for KCc1/CCc1 too, at the same moment —
  a market-hours artifact, not LRC-specific). SETTLE is always populated.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _common as c

DB_DIR   = Path(__file__).parent.parent / "Database"
DASH_DIR = Path(__file__).parent.parent / "Dashboard"

COMMODITY         = "LRC"
ATM_RIC           = "LRCc1"
ATM_FIELD         = "SETTLE"
# LRC's RIC-prefix search (no "1" disambiguator) also matches unrelated OPRA
# equity options (Lam Research Corp) sharing the same root — true match count
# (12,357) exceeds LSEG's 10,000-row search cap without this. Confirmed live
# 2026-09-17: ExchangeCode='IEU' (ICE Europe) cuts it to 3,207, safely under.
EXCHANGE_CODE     = "IEU"
STRIKE_GAP        = 25      # $/tonne
STRIKE_STEPS      = 30      # ATM +/- 30 -> 61 strikes, +/-750 $/tonne
STRIKE_MULTIPLIER = 1       # raw $/tonne, no *100 encoding (confirmed live)
RIC_PREFIX        = ""      # no "1" disambiguator (confirmed live)
ALLOWED_MONTHS    = {1, 3, 5, 7, 9, 11}   # Jan/Mar/May/Jul/Sep/Nov only
MONTHS_FORWARD    = 8       # ~4 years of listed expiries at 6 months/year... actually
                            # 8 expiries = ~16 months of real calendar time forward
BACKFILL_DAYS     = 90
ROLLING_DAYS      = 10
BATCH_SIZE        = 50
# Hard cap on strike count, nearest to ATM kept — same rate-limit control
# applied across all 6 commodities.
MAX_STRIKES       = 100

PARQUET_PATH = DB_DIR / "LRC_options_ice.parquet"
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
    parser.add_argument("--no-topup", action="store_true",
                        help="Skip the post-save real-time-quote OI top-up for the prior session")
    parser.add_argument("--active-only", action="store_true",
                        help="Fetch only RICs that carried OI or volume in the last 10 days. "
                             "Cuts the run's RIC count (and rate-limit exposure); settle-only "
                             "wings then refresh whenever you run a full (non-active-only) sweep.")
    parser.add_argument("--strike-skip", type=int, default=0,
                        help="Skip this many strikes nearest to ATM before applying MAX_STRIKES — "
                             "fetches the NEXT band outward for a staged rollout.")
    parser.add_argument("--max-strikes", type=int, default=None,
                        help="Override MAX_STRIKES for this run (the '+N more' pass of a staged rollout).")
    args = parser.parse_args()

    log = c.make_logger("lrc_ingest_lseg", Path(__file__).parent / "logs")
    c.run_ingest(
        commodity=COMMODITY, atm_ric=ATM_RIC, strike_gap=STRIKE_GAP,
        strike_steps=STRIKE_STEPS, strike_multiplier=STRIKE_MULTIPLIER,
        months_forward=MONTHS_FORWARD, backfill_days=BACKFILL_DAYS,
        rolling_days=ROLLING_DAYS, batch_size=BATCH_SIZE,
        parquet_path=PARQUET_PATH, atm_json=ATM_JSON, log=log,
        force_full=args.full, use_discovery=not args.legacy_window,
        include_weeklies=args.weeklies, require_oi=args.require_oi,
        dry_run=args.dry_run, days=args.days, ric_prefix=RIC_PREFIX,
        allowed_months=ALLOWED_MONTHS, atm_field=ATM_FIELD, no_topup=args.no_topup,
        exchange_code=EXCHANGE_CODE, active_only=args.active_only,
        max_strikes=(args.max_strikes if args.max_strikes is not None else MAX_STRIKES),
        strike_skip=args.strike_skip,
    )


if __name__ == "__main__":
    main()
