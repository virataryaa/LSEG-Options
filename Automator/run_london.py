"""
run_london.py — daily automator for the London-listed options (LRC, LCC).

Scheduled separately from run_nyc.py (~12:30pm vs ~4:10pm) because London and
NYC options settle/publish on different clocks. Run via run_london.bat.
"""

import datetime
import traceback

import _automator_common as ac

if __name__ == "__main__":
    try:
        ac.run_group("London", ["LRC", "LCC"])
    except Exception:
        msg = traceback.format_exc()
        ac.log(f"UNHANDLED ERROR:\n{msg}")
        ac.send_email_html(f"[LSEG Options] London CRASHED {datetime.date.today()}",
                           f"<pre>{msg}</pre>")
        raise
