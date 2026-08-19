# Options — Interim Migration (LSEG)

Interim replacement for `ICEBREAKER/Options`, rebuilt against the **LSEG
Data API** (`lseg.data`) instead of ICE Connect (`icepython`), for the
period while ICE API access is unavailable.

## Scope — KC only, by design

Per direct instruction, this migration is being built **one commodity at a
time**: KC first, confirm it's solid, then CC/SB/CT. Only KC is built so
far. The Dashboard (copied verbatim from the ICE source) already handles
this gracefully — each commodity's parquet load is wrapped in `_try_load()`,
which shows an `st.warning()` and falls back to an empty frame instead of
crashing when a commodity's file doesn't exist yet, so shipping KC-only is
safe. LCC was never integrated into the ICE source either (orphaned) and is
out of scope here too, unless asked for later.

## Where the RIC scheme came from

Confirmed empirically against live LSEG data (`discovery.search` +
`get_history`), using `Non Fundamental/Options/Code/ingest.py` (an earlier,
unfinished prototype) as a starting reference rather than copying it as-is,
since that project was flagged as never having been finished properly.

- **RIC form:** `1<root><strike*100><month_code><2-digit year>`, e.g.
  `1KC35000J26` = KC Oct 2026 350.00 Call. Standard OCC-style month letters:
  A–L = Jan–Dec calls, M–X = Jan–Dec puts.
- **ATM reference:** `KCc1` (front-month continuation), matching ICE's own
  `"KC 1!"` convention in `ICEBREAKER/Options/Code/Ingest.py`. The old LSEG
  prototype used `KCc2` (2nd nearby) instead — deliberately not carried
  over, since ICE's own convention is the more faithful match and the
  prototype was known-unfinished.
- **Strike/month universe:** ATM ± 20 strikes @ 2.50 gap, 12 forward
  months — same construction as ICE's `Ingest.py`.
- **Fields:** `SETTLE`, `OPINT_1` (open interest), `ACVOL_UNS` (volume),
  `IMP_VOLT` (implied volatility). IV was never pulled by the old prototype;
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

- **`Code/kc_ingest_lseg.py`** — KC ingest. Builds the ATM/strike/month
  universe, pre-filters to strikes with live open interest (batched
  `get_data(["OPINT_1"])` calls, mirrors ICE's own pre-filter approach),
  then pulls history in batches of 50 RICs. `--full` does a 90-day backfill
  from scratch; without it, only the last `ROLLING_DAYS` (10) are
  re-fetched and merged/deduped against the existing parquet. Also
  maintains `Dashboard/atm.json`, merging in just the `KC` key so the file
  stays forward-compatible with CC/SB/CT once those are added.
- **`Database/KC_options_ice.parquet`** — first full backfill: 6,600 rows,
  2026-05-21 → 2026-08-18, 131 live RICs (101 calls / 30 puts), impvol
  populated on 6,375/6,600 rows (96.6%).
- **`Dashboard/app.py`**, **`requirements.txt`** — copied verbatim from
  `ICEBREAKER/Options/Dashboard`, no code changes. Verified via Streamlit
  `AppTest` against the new KC-only data: renders with zero exceptions,
  all KC tabs/charts/tables populate, CC/SB/CT tabs show the expected
  "no data" warning instead of crashing.
- **`Automator/run_updater.py`**, **`run.bat`** — daily driver: runs the KC
  ingest, git add/commit/push the updated parquet + atm.json, emails a
  status summary via Outlook COM. Unlike ICE's `run.py`, which treats
  `returncode == 0` as success even on a genuinely empty/failed run,
  `kc_ingest_lseg.py` exits non-zero on real failure (no live RICs
  resolved, no rows returned), so the returncode check here is meaningful
  rather than a false signal.

## Validation

Direct RIC-level comparison against ICE's archived `KC_options_ice.parquet`
wasn't possible: **zero RIC/date overlap**. This is expected, not a bug —
both pipelines build an ATM-relative strike/month universe fresh on every
run, and ICE's archived file is stale (last written weeks earlier, under a
different ATM/strike regime, before the ICE pipeline went down ~3.5 weeks
ago). So validation instead relied on a plausibility check of the new
data's own value ranges and one fully-populated RIC's daily series:

- settle 0.38 – 146.47, impvol 29.81% – 68.91%, OI 0 – 4,917, volume 1 –
  2,251 — all within sane bounds for coffee options.
- `1KC31250C27` (the most complete series in the backfill, 61 rows,
  2026-05-21 → 2026-08-18): settle/OI/volume/impvol move smoothly day to
  day with no discontinuities or garbage values; the one real jump in the
  series lines up with the same coffee-price volatility already confirmed
  (against ICE's own archive) elsewhere in this migration set.

## Running it

```bash
python Code/kc_ingest_lseg.py           # incremental (last 10 days)
python Code/kc_ingest_lseg.py --full    # full rebuild, 90-day backfill
streamlit run Dashboard/app.py
```

Requires an authenticated LSEG Workspace/Eikon session on the host running
the ingest script.

## Next steps

- Once KC is confirmed working end-to-end, build CC, SB, CT the same way.
- GitHub repo + Streamlit Cloud deploy + homepage (`icebreaker.html`) link,
  once KC (or the fuller set) is ready to ship.
- Task Scheduler (cmd.exe basic task calling `run.bat`, "run only when
  logged on") — after a health-check run.
