"""
run_updater.py — RETIRED. Do not use.

This was the original single all-6-commodity automator. It was replaced by
run_london.py (LRC/LCC, ~12:30pm) and run_nyc.py (KC/SB/CT/CC, ~4:10pm),
which share _automator_common.py and pass --active-only on every commodity.

Found live 2026-09-21: the OLD scheduled task pointing at this file (via
run.bat) was never disabled, so it has been firing on its own schedule
EVERY DAY since the split, right alongside the two new automators —
meaning every commodity was being fetched via TWO separate full runs a
day, with this one never getting the --active-only trim. That is a very
plausible major contributor to the LSEG rate-limit exhaustion this
project hit starting 2026-09-17.

This file now does nothing but log that it was invoked and exit
immediately, so if the old Task Scheduler entry is still active, it can
no longer hit LSEG at all. FIND AND DELETE (or disable) THE SCHEDULED
TASK POINTING AT run.bat — this stub only stops the API calls, it does
not stop the wasted daily task-scheduler slot.
"""

import datetime
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "run_log.txt"


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    log("=" * 50)
    log("run_updater.py was invoked but is RETIRED — doing nothing, no LSEG calls made.")
    log("This means a scheduled task still points at run.bat / run_updater.py.")
    log("Find it in Task Scheduler and delete/disable it — use run_london.bat "
        "(~12:30pm) and run_nyc.bat (~4:10pm) instead.")
    log("=" * 50)
