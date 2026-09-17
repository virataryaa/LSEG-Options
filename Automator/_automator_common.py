"""
_automator_common.py — shared plumbing for the two options automators.

The single six-commodity automator (run_updater.py) is split in two because
London-listed (LRC, LCC) and NYC-listed (KC, SB, CC, CT) options settle and
publish on different schedules, so the desk wants them run at different times
of day: run_london.py (~12:30pm) and run_nyc.py (~4:10pm), each via its own
cmd.exe scheduled task and .bat file — see run_london.bat / run_nyc.bat.

Both groups share this module so the ingest/git/email logic only exists once.
"""

import shutil
import subprocess
import sys
import time
import datetime
import traceback
from pathlib import Path

import pandas as pd
import win32com.client

ROOT     = Path(__file__).resolve().parent.parent
CODE_DIR = ROOT / "Code"
DB_DIR   = ROOT / "Database"
LOG_FILE = Path(__file__).resolve().parent / "run_log.txt"
ATM_JSON = ROOT / "Dashboard" / "atm.json"
PYTHON   = sys.executable
EMAIL_TO = "virat.arya@etgworld.com"

# All 6, for the full-board status table shown in every email regardless of
# which group actually ran — so the recipient always sees the whole book's
# state, not just the half that was touched this run.
ALL_COMMODITIES = {
    "LRC": (CODE_DIR / "lrc_ingest_lseg.py", DB_DIR / "LRC_options_ice.parquet", "London"),
    "LCC": (CODE_DIR / "lcc_ingest_lseg.py", DB_DIR / "LCC_options_ice.parquet", "London"),
    "KC":  (CODE_DIR / "kc_ingest_lseg.py",  DB_DIR / "KC_options_ice.parquet",  "NYC"),
    "SB":  (CODE_DIR / "sb_ingest_lseg.py",  DB_DIR / "SB_options_ice.parquet",  "NYC"),
    "CT":  (CODE_DIR / "ct_ingest_lseg.py",  DB_DIR / "CT_options_ice.parquet",  "NYC"),
    "CC":  (CODE_DIR / "cc_ingest_lseg.py",  DB_DIR / "CC_options_ice.parquet",  "NYC"),
}
COOLDOWN_SECONDS = 60  # pause between commodities in a group to let the rate-limit window reset

FUTURES_SRC = Path(r"C:\Users\virat.arya\ETG\SoftsDatabase - Documents\Database\Hardmine\LSEG\Futures\Database")
FUTURES_DST = DB_DIR / "Futures"
FUTURES_MAP = {
    "kc_futures.parquet":  "kc_futures.parquet",
    "cc_futures.parquet":  "cc_futures.parquet",
    "sb_futures.parquet":  "sb_futures.parquet",
    "ct_futures.parquet":  "ct_futures.parquet",
    "lcc_futures.parquet": "lcc_futures.parquet",
    "rc_futures.parquet":  "lrc_futures.parquet",
}


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def send_email_html(subject: str, html_body: str):
    try:
        ol   = win32com.client.Dispatch("Outlook.Application")
        mail = ol.CreateItem(0)
        mail.To       = EMAIL_TO
        mail.Subject  = subject
        mail.HTMLBody = html_body
        mail.Send()
        log("Email sent.")
    except Exception as e:
        log(f"Email failed: {e}")


def sync_futures() -> tuple[bool, str]:
    FUTURES_DST.mkdir(parents=True, exist_ok=True)
    lines = []
    ok = True
    for src_name, dst_name in FUTURES_MAP.items():
        src = FUTURES_SRC / src_name
        dst = FUTURES_DST / dst_name
        if not src.exists():
            lines.append(f"  MISSING: {src}")
            ok = False
            continue
        shutil.copy2(src, dst)
        lines.append(f"  {src_name} -> {dst_name}")
    return ok, "\n".join(lines)


def run_ingest(script: Path, label: str) -> tuple[bool, str]:
    log(f"Running {label} ingest...")
    result = subprocess.run([PYTHON, str(script)], capture_output=True, text=True)
    output = result.stdout + result.stderr
    # kc_ingest_lseg.py / _common.py's run_ingest sys.exit(1) on genuine
    # failure (no live RICs, no data returned) rather than silently exiting 0
    # with "nothing to save" — so returncode==0 here is a reliable signal.
    return result.returncode == 0, output


def git_push(files: list[Path]) -> tuple[bool, str]:
    rel = [str(f.relative_to(ROOT)) for f in files if f.exists()]
    if not rel:
        return False, "No files to stage"
    cmds = [
        ["git", "add"] + rel,
        ["git", "commit", "-m", f"auto: options update (LSEG) {datetime.date.today()}"],
        ["git", "push"],
    ]
    out = ""
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        out += r.stdout + r.stderr
        if r.returncode != 0 and "nothing to commit" not in r.stderr:
            return False, out
    return True, out


def _commodity_snapshot(key: str, parquet_path: Path) -> dict:
    """Read one commodity's current parquet for the status table. Never raises —
    a read failure shows as a row saying so instead of crashing the whole email."""
    if not parquet_path.exists():
        return dict(key=key, ok=False, note="no parquet yet")
    try:
        df = pd.read_parquet(parquet_path)
        df["date"] = pd.to_datetime(df["date"])
        last = df["date"].max()
        oi_mask = df["oi"].notna()
        oi_date = df.loc[oi_mask, "date"].max() if oi_mask.any() else None
        stale_days = (pd.Timestamp.today().normalize() - last).days
        return dict(
            key=key, ok=True,
            rows=len(df), rics=df["ric"].nunique(),
            calls=df.loc[df["option_type"] == "Call", "ric"].nunique(),
            puts=df.loc[df["option_type"] == "Put", "ric"].nunique(),
            n_exp=df[["expiry_month", "expiry_year"]].drop_duplicates().shape[0],
            lo=df["strike"].min(), hi=df["strike"].max(),
            last=last.date(), oi_date=(oi_date.date() if oi_date is not None else None),
            ivpct=round(df["impvol"].notna().mean() * 100, 1) if "impvol" in df.columns else None,
            stale_days=stale_days,
        )
    except Exception as e:
        return dict(key=key, ok=False, note=str(e)[:150])


# Outlook's HTML renderer is Word's engine: inline styles only, plain <table>
# layout, no flexbox/grid/custom properties/media queries — all of that is
# silently dropped or breaks. Kept deliberately old-school for that reason.
_TH = ('padding:6px 10px;border:1px solid #ccc;background:#2a2a2a;color:#fff;'
       'font-size:12px;text-align:left;white-space:nowrap;')
_TD = 'padding:6px 10px;border:1px solid #ccc;font-size:12px;white-space:nowrap;'


def build_status_table_html(ran_this_run: set) -> str:
    """Full 6-commodity status table: fields x commodities, latest date per field.
    `ran_this_run` marks which commodities this specific run touched, so the
    recipient can tell fresh-this-run rows from carried-over ones at a glance.
    """
    rows_html = []
    for key, (_script, parquet, group) in ALL_COMMODITIES.items():
        s = _commodity_snapshot(key, parquet)
        ran = key in ran_this_run
        name_bg = "#eef6ff" if ran else "#ffffff"
        if not s["ok"]:
            rows_html.append(
                f'<tr style="background:{name_bg}"><td style="{_TD}"><b>{key}</b> ({group})</td>'
                f'<td style="{_TD};color:#a3271f" colspan="9">{s.get("note","no data")}</td></tr>')
            continue
        stale = s["stale_days"]
        status = ("OK" if stale <= 1 else
                   f"{stale}d stale" if stale <= 10 else
                   f"{stale}d STALE — needs --full")
        status_color = "#3c7a41" if stale <= 1 else ("#b4590c" if stale <= 10 else "#a3271f")
        ran_tag = " &#9679;" if ran else ""
        rows_html.append(
            '<tr style="background:%s">'
            '<td style="%s"><b>%s</b>%s</td><td style="%s">%s</td>'
            '<td style="%s">%s</td><td style="%s">%s / %s</td>'
            '<td style="%s">%s</td><td style="%s">%s&ndash;%s</td>'
            '<td style="%s">%s</td><td style="%s">%s</td>'
            '<td style="%s">%s%%</td>'
            '<td style="%scolor:%s"><b>%s</b></td>'
            '</tr>' % (
                name_bg,
                _TD, key, ran_tag, _TD, group,
                _TD, f"{s['rows']:,}", _TD, s["calls"], s["puts"],
                _TD, s["n_exp"], _TD, s["lo"], s["hi"],
                _TD, s["last"], _TD, s["oi_date"] or "&mdash;",
                _TD, s["ivpct"],
                _TD, status_color, status,
            ))

    header = ''.join(f'<th style="{_TH}">{h}</th>' for h in
                      ["Commodity", "Group", "Rows", "Calls/Puts", "Expiries",
                       "Strike range", "Latest data", "Latest OI", "ImpVol", "Status"])
    return (
        '<p style="font-family:Calibri,Arial,sans-serif;font-size:13px;color:#333;margin:0 0 10px">'
        '&#9679; = refreshed in this run</p>'
        f'<table style="border-collapse:collapse;font-family:Calibri,Arial,sans-serif">'
        f'<thead><tr>{header}</tr></thead><tbody>{"".join(rows_html)}</tbody></table>'
    )


def build_email_html(group_label: str, today: str, fut_ok: bool, fut_out: str,
                     results: dict, pushed: bool, git_out: str) -> str:
    f = "font-family:Calibri,Arial,sans-serif"
    table = build_status_table_html(set(results.keys()))

    detail_blocks = []
    for label, (ok, out) in results.items():
        color = "#3c7a41" if ok else "#a3271f"
        detail_blocks.append(
            f'<p style="{f};font-size:13px;margin:14px 0 4px"><b>{label}</b> '
            f'<span style="color:{color}">({"OK" if ok else "FAILED"})</span></p>'
            f'<pre style="background:#f5f5f5;border:1px solid #ddd;padding:8px;'
            f'font-size:11px;white-space:pre-wrap;font-family:Consolas,monospace">'
            f'{out.strip()}</pre>')

    return f"""
    <div style="{f};font-size:14px;color:#1a1a1a">
      <h2 style="{f};font-size:16px;margin:0 0 4px">Options Ingest (LSEG) &mdash; {group_label}</h2>
      <p style="{f};font-size:12px;color:#666;margin:0 0 16px">Run {today}</p>
      {table}
      <p style="{f};font-size:13px;margin:16px 0 4px">
        <b>Futures sync:</b> <span style="color:{'#3c7a41' if fut_ok else '#a3271f'}">
        {'OK' if fut_ok else 'PARTIAL/FAILED'}</span></p>
      <pre style="background:#f5f5f5;border:1px solid #ddd;padding:8px;font-size:11px;
        white-space:pre-wrap;font-family:Consolas,monospace">{fut_out}</pre>
      <p style="{f};font-size:13px;margin:16px 0 4px">
        <b>Git:</b> <span style="color:{'#3c7a41' if pushed else '#a3271f'}">
        {'pushed' if pushed else 'nothing new / failed'}</span></p>
      <pre style="background:#f5f5f5;border:1px solid #ddd;padding:8px;font-size:11px;
        white-space:pre-wrap;font-family:Consolas,monospace">{git_out.strip()}</pre>
      <h3 style="{f};font-size:13px;margin:20px 0 4px">Per-commodity detail</h3>
      {''.join(detail_blocks)}
    </div>
    """


def run_group(group_label: str, commodity_keys: list[str]):
    """Runs sync_futures + the given commodities (with cooldown between each),
    pushes to git, and emails an HTML report. Shared by run_london.py / run_nyc.py."""
    today = datetime.date.today().isoformat()
    log("=" * 50)
    log(f"Options ingest (LSEG) — {group_label} — started {today}")

    fut_ok, fut_out = sync_futures()
    log(f"Futures sync: {'OK' if fut_ok else 'PARTIAL/FAILED'}")

    results = {}
    any_failed = False
    for idx, key in enumerate(commodity_keys):
        script, _parquet, _group = ALL_COMMODITIES[key]
        if idx > 0:
            log(f"Cooldown {COOLDOWN_SECONDS}s before {key}...")
            time.sleep(COOLDOWN_SECONDS)
        ok, out = run_ingest(script, key)
        results[key] = (ok, out)
        log(f"{key} ingest: {'OK' if ok else 'FAILED'}")
        if not ok:
            any_failed = True

    files_to_push = [ALL_COMMODITIES[k][1] for k in commodity_keys] + [ATM_JSON]
    if any(f.exists() for f in [FUTURES_DST / n for n in FUTURES_MAP.values()]):
        files_to_push += [FUTURES_DST / n for n in FUTURES_MAP.values()]
    pushed, git_out = git_push(files_to_push)
    log("Git push: OK" if pushed else "Git push: FAILED (may be nothing new)")

    html = build_email_html(group_label, today, fut_ok, fut_out, results, pushed, git_out)
    subject = f"[LSEG Options] {group_label} {'PARTIAL FAIL' if any_failed else 'OK'} {today}"
    send_email_html(subject, html)
    log("Done.")

    if any_failed:
        sys.exit(1)
