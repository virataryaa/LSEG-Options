"""
run_nyc.py — daily automator for the NYC-listed options (KC, SB, CT, CC).

Scheduled separately from run_london.py (~4:10pm vs ~12:30pm) because London
and NYC options settle/publish on different clocks. Run via run_nyc.bat.
Ordered smallest-universe-first (KC < SB < CT < CC) so a rate-limit throttle
building up over the run never starves the biggest board (CC) of its whole
run the way it did on 2026-09-15.
"""

import datetime
import traceback

import _automator_common as ac

if __name__ == "__main__":
    try:
        ac.run_group("NYC", ["KC", "SB", "CT", "CC"])
    except Exception:
        msg = traceback.format_exc()
        ac.log(f"UNHANDLED ERROR:\n{msg}")
        ac.send_email_html(f"[LSEG Options] NYC CRASHED {datetime.date.today()}",
                           f"<pre>{msg}</pre>")
        raise
