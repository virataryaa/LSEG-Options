"""
run_updater.py — Options (LSEG interim migration) daily automator
Runs all four commodities (KC/CC/SB/CT) sequentially, mirroring
ICEBREAKER/Options/Automator/run.py's structure (commit/push/email shape).
Each commodity's ingest is independent — one failing doesn't stop the rest,
but any failure marks the whole run as failed for the email subject/exit code.
"""

import subprocess
import sys
import datetime
import traceback
from pathlib import Path

import win32com.client

ROOT     = Path(__file__).resolve().parent.parent
CODE_DIR = ROOT / "Code"
LOG_FILE = Path(__file__).resolve().parent / "run_log.txt"
PYTHON   = sys.executable

COMMODITIES = {
    "KC": (CODE_DIR / "kc_ingest_lseg.py", ROOT / "Database" / "KC_options_ice.parquet"),
    "CC": (CODE_DIR / "cc_ingest_lseg.py", ROOT / "Database" / "CC_options_ice.parquet"),
    "SB": (CODE_DIR / "sb_ingest_lseg.py", ROOT / "Database" / "SB_options_ice.parquet"),
    "CT": (CODE_DIR / "ct_ingest_lseg.py", ROOT / "Database" / "CT_options_ice.parquet"),
}
ATM_JSON = ROOT / "Dashboard" / "atm.json"

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

    results = {}
    any_failed = False
    for label, (script, _parquet) in COMMODITIES.items():
        ok, out = run_ingest(script, label)
        results[label] = (ok, out)
        log(f"{label} ingest: {'OK' if ok else 'FAILED'}")
        for line in out.strip().splitlines():
            log(f"  {line}")
        if not ok:
            any_failed = True

    files_to_push = [p for _s, p in COMMODITIES.values()] + [ATM_JSON]
    pushed, git_out = git_push(files_to_push)
    log("Git push: OK" if pushed else "Git push: FAILED (may be nothing new)")
    for line in git_out.strip().splitlines():
        log(f"  {line}")

    body_parts = [f"Options ingest (LSEG) completed — {today}\n"]
    for label, (ok, out) in results.items():
        body_parts.append(f"=== {label} ({'OK' if ok else 'FAILED'}) ===\n{out.strip()}\n")
    body_parts.append(f"Git: {'pushed' if pushed else 'nothing new / failed'}\n{git_out.strip()}")
    body = "\n".join(body_parts)

    subject = f"[Interim_Migration Options] {'PARTIAL FAIL' if any_failed else 'OK'} {today}"
    send_email(subject, body)
    log("Done.")

    if any_failed:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        msg = traceback.format_exc()
        log(f"UNHANDLED ERROR:\n{msg}")
        send_email(f"[Interim_Migration Options] CRASHED {datetime.date.today()}", msg)
        sys.exit(1)
