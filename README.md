# Options — Interim Migration (LSEG)

Interim replacement for `ICEBREAKER/Options`, rebuilt against the **LSEG
Data API** (`lseg.data`) instead of ICE Connect (`icepython`), for the
period while ICE API access is unavailable.

## Scope — all four commodities now built

Per direct instruction, this migration was built **one commodity at a
time**: KC first, confirmed solid, then CC/SB/CT built the same way once KC
was validated. All four are now live. The Dashboard (copied verbatim from
the ICE source) handled the KC-only interim period gracefully — each
commodity's parquet load is wrapped in `_try_load()`, showing an
`st.warning()` and falling back to an empty frame instead of crashing when
a file doesn't exist — and with all four parquets now present, those
warnings no longer appear. LCC was never integrated into the ICE source
either (orphaned) and remains out of scope here too, unless asked for later.

## Where the RIC scheme came from

Confirmed empirically against live LSEG data (`discovery.search` +
`get_history`), using `Non Fundamental/Options/Code/ingest.py` (an earlier,
unfinished prototype, KC-only) as a starting reference rather than copying
it as-is, since that project was flagged as never having been finished
properly. CC/SB/CT had no LSEG prototype to draw on at all — their RIC
scheme, strike units and grid step were each discovered from scratch via
`discovery.search`, one commodity at a time, and confirmed live with
`get_history` before being wired into the ingest scripts.

- **RIC form:** `1<root><strike_encoded><month_code><2-digit year>`, e.g.
  `1KC35000J26` = KC Oct 2026 350.00 Call. Standard OCC-style month letters:
  A–L = Jan–Dec calls, M–X = Jan–Dec puts. **The strike encoding is not
  uniform across commodities** — this was the one real surprise in
  extending beyond KC:
  - KC / SB / CT: strike × 100 (cents-style encoding), e.g. `1SB1600J26` =
    16.00 cts/lb, `1CT8000L26` = 80.00 cts/lb.
  - CC: strike used **as-is**, no ×100 — LSEG's CC option grid is natively
    in whole $/mt (e.g. `1CC6100L26` = 6100 $/mt), matching `CCc1`'s own
    units directly. ICE's `CC_Ingest.py` quotes and converts through
    $/cwt; LSEG needs no such conversion here since both the futures and
    the options strikes are already $/mt.
  - This was caught by an early bug: the shared ingest logic initially
    scaled the ATM price by the strike-encoding multiplier *before*
    snapping it to the strike grid, which is only correct if the multiplier
    also matches the grid's own scale. It happened to work for KC/SB/CT but
    silently produced a 100×-too-high ATM for SB on first run (aborted
    safely with "no live RICs found" rather than writing bad data — the
    ATM-strike prefilter step caught it). Fixed by keeping strikes in real
    price units throughout and applying the ×100 encoding only at RIC-string
    build time, matching the original KC script's approach.
- **ATM reference:** front-month continuation (`KCc1`/`CCc1`/`SBc1`/`CTc1`),
  matching ICE's own `"<root> 1!"` convention in
  `ICEBREAKER/Options/Code/*_Ingest.py`. The old KC-only LSEG prototype used
  `KCc2` (2nd nearby) instead — deliberately not carried over, since ICE's
  own convention is the more faithful match and the prototype was
  known-unfinished.
- **Strike/month universe:** 12 forward months for all four. Strike wings
  differ per commodity's own tick size and typical range — matched to (KC/
  SB/CT) or scaled from (CC) the ICE source's own wing choices:
  | Commodity | Strike gap | Wing (±strikes) | Encoding |
  |---|---|---|---|
  | KC | 2.5 cts/lb | 20 | ×100 |
  | CC | 50 $/mt | 40 | ×1 |
  | SB | 0.25 cts/lb | 20 | ×100 |
  | CT | 1 ct/lb | 20 | ×100 |
- **Fields:** `SETTLE`, `OPINT_1` (open interest), `ACVOL_UNS` (volume),
  `IMP_VOLT` (implied volatility) — same four fields resolve cleanly for
  all four commodities. IV was never pulled by the old KC prototype;
  `IMP_VOLT` was found by probing candidate field names against a live
  snapshot (`IVOL`, `IMPL_VOLT`, `OPT_IMP_VOLT`, `IMPLIEDVOL`, `IMPVOL`,
  `VOLATILTY`, `BLKSCH_VOL`, `OPT_VOL_MID` all failed with "Field not
  found"; `IMP_VOLT` succeeded and returns a plausible IV% value).

## No T+1 shift

ICE's `Ingest.py` applies a `groupby("ric").shift(-1)` to Open Interest and
Volume, because ICE's own OI/Volume timestamps lag one day. Per direct
instruction, **this migration does not replicate that shift** — LSEG's
OI/Volume are used exactly as published, since the one-day mismatch between
the two sources is a known, already-understood ICE-side quirk rather than
something to correct for on the LSEG side.

## A testing mistake worth flagging for future readers

Early on, `get_history()` on an option RIC returned "universe not found"
for every field, which looked like a hard platform limitation (no
historical time series for individual options — only live snapshots via
`get_data()`). That conclusion was wrong: the RIC being tested
(`1KC28500I26`) referenced an **already-expired** September 2026 contract,
carried over from stale historical parquet data. Once expired, a contract's
RIC simply stops resolving in `get_history` — normal instrument-lifecycle
behavior, not a systemic limitation. Testing against a currently-listed,
non-expired RIC (`1KC35000J26`) returned clean multi-day history
immediately. The production ingest never hits this problem because it
always generates its strike/month universe fresh off today's date — but if
you're ever debugging a "no history" error by hand, check contract expiry
first.

## What's here

- **`Code/_common.py`** — shared ingest logic used by CC/SB/CT (ATM/strike/
  month universe construction, OI pre-filter, batched history fetch,
  upsert). `kc_ingest_lseg.py` is deliberately **not** retrofitted onto this
  module — it was already validated and pushed before CC/SB/CT existed, and
  there was no reason to touch a proven script. The duplication between it
  and `_common.py` is intentional, not an oversight.
- **`Code/kc_ingest_lseg.py`**, **`cc_ingest_lseg.py`**, **`sb_ingest_lseg.py`**,
  **`ct_ingest_lseg.py`** — one ingest per commodity. Each pre-filters to
  strikes with live open interest (batched `get_data(["OPINT_1"])` calls,
  mirrors ICE's own pre-filter approach), then pulls history in batches of
  50 RICs. `--full` does a 90-day backfill from scratch; without it, only
  the last `ROLLING_DAYS` (10) are re-fetched and merged/deduped against
  the existing parquet. Each also maintains its own key in the shared
  `Dashboard/atm.json`.
- **`Database/{KC,CC,SB,CT}_options_ice.parquet`** — first full backfills:

  | Commodity | Rows | Date range | Live RICs | Calls / Puts | impvol coverage |
  |---|---|---|---|---|---|
  | KC | 6,600 | 2026-05-21 → 2026-08-18 | 131 | 101 / 30 | 96.6% |
  | CC | 14,920 | 2026-05-21 → 2026-08-18 | 286 | 172 / 114 | 97.1% |
  | SB | 22,875 | 2026-05-21 → 2026-08-18 | 417 | 225 / 192 | 95.6% |
  | CT | 22,265 | 2026-05-21 → 2026-08-18 | 365 | 217 / 148 | 97.6% |

- **`Dashboard/app.py`**, **`requirements.txt`** — copied verbatim from
  `ICEBREAKER/Options/Dashboard`, no code changes. Verified via Streamlit
  `AppTest` against the full four-commodity dataset: 16 tabs (4 per
  commodity), 11 charts, 8 dataframes, **zero exceptions and zero
  warnings** — every commodity now has real data, so the `_try_load()`
  "no data" fallback path is no longer exercised in normal operation.
- **`Automator/run_updater.py`**, **`run.bat`** — daily driver: runs all
  four ingests sequentially (one commodity failing doesn't stop the
  others), git add/commit/push all four parquets + atm.json, emails a
  status summary via Outlook COM (subject flags `PARTIAL FAIL` if any
  commodity failed). Unlike ICE's `run.py`, which treats `returncode == 0`
  as success even on a genuinely empty/failed run, each `*_ingest_lseg.py`
  exits non-zero on real failure (no live RICs resolved, no rows
  returned), so the returncode check here is meaningful rather than a
  false signal.

## Validation

Direct RIC-level comparison against ICE's archived parquets wasn't
possible for any commodity: **zero RIC/date overlap** across the board.
This is expected, not a bug — both pipelines build an ATM-relative
strike/month universe fresh on every run, and ICE's archived files are
stale (last written weeks earlier, under a different ATM/strike regime,
before the ICE pipeline went down ~3.5 weeks ago). Validation instead
relied on plausibility checks of each commodity's own value ranges and,
for KC, one fully-populated RIC's daily series:

- KC: settle 0.38 – 146.47, impvol 29.81% – 68.91%, OI 0 – 4,917, volume
  1 – 2,251. `1KC31250C27` (61 rows, 2026-05-21 → 2026-08-18) moves
  smoothly day to day with no discontinuities; the one real jump lines up
  with the same coffee-price volatility already confirmed (against ICE's
  own archive) elsewhere in this migration set.
- CC: settle 1.00 – 2,796.00 ($/mt), impvol 25.5% – 93.6%, strikes 3,900 –
  7,800 ($/mt), bracketing the CCc1 ATM of 5,900.
- SB: settle 0.01 – 8.16 (cts/lb), impvol 15.2% – 332.6%, strikes 12.5 –
  22.5, bracketing the SBc1 ATM of 17.5.
- CT: settle 0.01 – 29.88 (cts/lb), impvol 12.6% – 103.9%, strikes 65 –
  105, bracketing the CTc1 ATM of 85.
- The extreme high-end impvol values for SB (332.6%) and CT (103.9%) both
  trace back to deep out-of-the-money puts settling at the minimum tick
  (0.01) — a classic implied-vol solver artifact at the price floor, not
  a data error. Confirmed by inspecting the actual rows: both had
  `settle == 0.01`, low/no OI, near-term expiry.

## Running it

```bash
python Code/kc_ingest_lseg.py           # incremental (last 10 days)
python Code/cc_ingest_lseg.py --full    # full rebuild, 90-day backfill
python Code/sb_ingest_lseg.py
python Code/ct_ingest_lseg.py
streamlit run Dashboard/app.py
```

Requires an authenticated LSEG Workspace/Eikon session on the host running
the ingest scripts.

## Next steps

- Deployed to GitHub (`virataryaa/interim-migration-Options`). Streamlit
  Cloud deploy + homepage (`icebreaker.html`) link, once a URL is available.
- Task Scheduler (cmd.exe basic task calling `run.bat`, "run only when
  logged on") — after a health-check run.
