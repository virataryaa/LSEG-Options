"""
run_updater.py — Options (LSEG interim migration) daily automator
KC only for now — mirrors ICEBREAKER/Options/Automator/run.py's structure
(same commit/push/email shape) but scoped down while only KC is built.
Add CC/SB/CT ingest calls here once those are migrated the same way.
"""

import subprocess
import sys
import datetime
import traceback
from pathlib import Path

import win32com.client

ROOT       = Path(__file__).resolve().parent.parent
INGEST_KC  = ROOT / "Code" / "kc_ingest_lseg.py"
PARQUET_KC = ROOT / "Database" / "KC_options_ice.parquet"
ATM_JSON   = ROOT / "Dashboard" / "atm.json"
LOG_FILE   = Path(__file__).resolve().parent / "run_log.txt"
PYTHON     = sys.executable

EMAIL_TO = "virat.arya@etgworld.com"


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_email(subject: str, body: str):
    try:
        ol   = win32com.client.Dispatch("Outlook.Application")
        mail = ol.CreateItem(0)
        mail.To      = EMAIL_TO
        mail.Subject = subject
        mail.Body    = body
        mail.Send()
        log("Email sent.")
    except Exception as e:
        log(f"Email failed: {e}")


def run_ingest(script: Path, label: str) -> tuple[bool, str]:
    log(f"Running {label} ingest...")
    result = subprocess.run([PYTHON, str(script)], capture_output=True, text=True)
    output = result.stdout + result.stderr
    # Unlike the ICE source, kc_ingest_lseg.py sys.exit(1)s on genuine failure
    # (no live RICs, no data returned) rather than silently exiting 0 with
    # "nothing to save" — so returncode==0 here is actually a reliable signal.
    return result.returncode == 0, output


def git_push(files: list[Path]) -> tuple[bool, str]:
    rel = [str(f.relative_to(ROOT)) for f in files if f.exists()]
    if not rel:
        return False, "No files to stage"
    cmds = [
        ["git", "add"] + rel,
        ["git", "commit", "-m", f"auto: daily options update (LSEG) {datetime.date.today()}"],
        ["git", "push"],
    ]
    out = ""
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        out += r.stdout + r.stderr
        if r.returncode != 0 and "nothing to commit" not in r.stderr:
            return False, out
    return True, out


def main():
    today = datetime.date.today().isoformat()
    log("=" * 50)
    log(f"Options ingest (LSEG) started — {today}")

    ok, out = run_ingest(INGEST_KC, "KC")
    log(f"KC ingest: {'OK' if ok else 'FAILED'}")
    for line in out.strip().splitlines():
        log(f"  {line}")

    if not ok:
        send_email(f"[Interim_Migration Options] FAILED {today} (KC)", out)
        sys.exit(1)

    pushed, git_out = git_push([PARQUET_KC, ATM_JSON])
    log("Git push: OK" if pushed else "Git push: FAILED (may be nothing new)")
    for line in git_out.strip().splitlines():
        log(f"  {line}")

    body = (
        f"Options ingest (LSEG) completed — {today}\n\n"
        f"=== KC ===\n{out.strip()}\n\n"
        f"Git: {'pushed' if pushed else 'nothing new / failed'}\n{git_out.strip()}"
    )
    send_email(f"[Interim_Migration Options] OK {today}", body)
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        msg = traceback.format_exc()
        log(f"UNHANDLED ERROR:\n{msg}")
        send_email(f"[Interim_Migration Options] CRASHED {datetime.date.today()}", msg)
        sys.exit(1)
