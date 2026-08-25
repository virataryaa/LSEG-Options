"""
common.py — Shared data loaders, pivot helpers, and rendering utilities
for the Options Dashboard apps.

Split from the original monolithic app.py so that:
  - app.py                  → OI Change + Volume only (fast, default view)
  - oi_advanced_analytics.py → Px Change, Vol Surface, IV vs RV

Each app runs as its own Streamlit process, so @st.cache_data caches are
NOT shared between them — but that's fine, since each app only loads/
computes what it actually renders.
"""

import json
import streamlit as st
import pandas as pd
import numpy as np
from pathlib import Path

DB_PATH  = Path(__file__).parent.parent / "Database"
ATM_JSON = Path(__file__).parent / "atm.json"
FUT_PATH = Path(__file__).parent.parent / "Database" / "Futures"

MONTH_NAMES    = {1:"Jan",2:"Feb",3:"Mar",4:"Apr",5:"May",6:"Jun",
                  7:"Jul",8:"Aug",9:"Sep",10:"Oct",11:"Nov",12:"Dec"}
CALL_CODES     = {1:"A",2:"B",3:"C",4:"D",5:"E",6:"F",7:"G",8:"H",9:"I",10:"J",11:"K",12:"L"}
PUT_CODES      = {1:"M",2:"N",3:"O",4:"P",5:"Q",6:"R",7:"S",8:"T",9:"U",10:"V",11:"W",12:"X"}
MONTH_TO_CODE  = {1:"F",2:"G",3:"H",4:"J",5:"K",6:"M",7:"N",8:"Q",9:"U",10:"V",11:"X",12:"Z"}
CODE_TO_MONTH_INT = {"F":1,"G":2,"H":3,"J":4,"K":5,"M":6,"N":7,"Q":8,"U":9,"V":10,"X":11,"Z":12}


# ── Data loaders ───────────────────────────────────────────────────────────────
@st.cache_data(ttl=1800)
def load_kc():
    df = pd.read_parquet(DB_PATH / "KC_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_cc():
    df = pd.read_parquet(DB_PATH / "CC_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_sb():
    df = pd.read_parquet(DB_PATH / "SB_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_ct():
    df = pd.read_parquet(DB_PATH / "CT_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_lrc():
    df = pd.read_parquet(DB_PATH / "LRC_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_lcc():
    df = pd.read_parquet(DB_PATH / "LCC_options_ice.parquet")
    df["date"] = pd.to_datetime(df["date"])
    return df

@st.cache_data(ttl=1800)
def load_fut(name: str) -> pd.DataFrame:
    """Load futures parquet for per-expiry ATM. Returns empty DF if unavailable (e.g. Streamlit Cloud)."""
    path = FUT_PATH / f"{name}_futures.parquet"
    try:
        df = pd.read_parquet(path)
        df["Date"] = pd.to_datetime(df["Date"])
        df["month_int"] = df["month"].map(CODE_TO_MONTH_INT)
        return df[["Date", "month_int", "year", "settlement"]].dropna(subset=["settlement"])
    except Exception:
        return pd.DataFrame()

def load_atm():
    try:
        with open(ATM_JSON) as f:
            return json.load(f)
    except Exception:
        return {}

def _try_load(fn, name):
    try:
        return fn()
    except Exception as e:
        st.warning(f"Could not load {name} data: {e}")
        return pd.DataFrame()


def load_core_data():
    """Loads option dataframes + ATM json. Shared by both apps."""
    df_kc  = _try_load(load_kc,  "KC")
    df_cc  = _try_load(load_cc,  "CC")
    df_sb  = _try_load(load_sb,  "SB")
    df_ct  = _try_load(load_ct,  "CT")
    df_lrc = _try_load(load_lrc, "LRC")
    df_lcc = _try_load(load_lcc, "LCC")
    atm_data = load_atm()
    return dict(KC=df_kc, CC=df_cc, SB=df_sb, CT=df_ct, LRC=df_lrc, LCC=df_lcc), atm_data


def render_sidebar(dfs, title="Options Dashboard"):
    """Shared Old Date / New Date picker + latest-data status. Returns (old_date, new_date)."""
    all_dates = set()
    for _df in [dfs["KC"], dfs["CC"], dfs["SB"], dfs["CT"]]:
        if not _df.empty:
            all_dates.update(_df["date"].dt.date.unique())
    available_dates = sorted(all_dates)

    with st.sidebar:
        st.title(title)
        st.divider()
        old_date = st.selectbox("Old Date", available_dates,
                                 index=max(0, len(available_dates) - 10),
                                 format_func=lambda d: d.strftime("%d %b %Y"))
        new_date = st.selectbox("New Date", available_dates,
                                 index=len(available_dates) - 1,
                                 format_func=lambda d: d.strftime("%d %b %Y"))
        if old_date == new_date:
            st.warning("Old Date and New Date are the same.")

        st.divider()
        st.markdown("**Latest data available**")
        for _label, _df in [("Arabica (KC)", dfs["KC"]), ("Robusta (LRC)", dfs["LRC"]),
                            ("NYC Cocoa (CC)", dfs["CC"]), ("London Cocoa (LCC)", dfs["LCC"]),
                            ("Sugar (SB)", dfs["SB"]), ("Cotton (CT)", dfs["CT"])]:
            if not _df.empty:
                _latest = _df["date"].max().date().strftime("%d %b %Y")
                st.caption(f"{_label} — {_latest}")
            else:
                st.caption(f"{_label} — no data")

    return old_date, new_date


# ── Pivot helpers (all parameterised) ─────────────────────────────────────────
def _month_keys(df):
    return (df[["expiry_month", "expiry_year"]]
            .drop_duplicates()
            .sort_values(["expiry_year", "expiry_month"])
            .apply(lambda r: (int(r.expiry_month), int(r.expiry_year)), axis=1)
            .tolist())

def _meta(df, opt):
    return (df[df["option_type"] == opt]
            [["ric", "strike", "expiry_month", "expiry_year"]]
            .drop_duplicates()
            .assign(mk=lambda x: list(zip(x.expiry_month.astype(int), x.expiry_year.astype(int))))
            .set_index("ric"))

def _clean(pivot, month_keys):
    if pivot.empty:
        return pivot
    pivot = pivot.reindex(columns=month_keys)
    return pivot.apply(lambda c: pd.to_numeric(c, errors="coerce")).astype(float)

def _valid(df, opt, new_date, min_oi):
    if min_oi <= 0:
        return None
    d2 = df[(df["date"].dt.date == new_date) & (df["option_type"] == opt)][["ric", "oi"]]
    return d2[pd.to_numeric(d2["oi"], errors="coerce") >= min_oi]["ric"]

@st.cache_data(ttl=1800)
def _change_pivot(df, month_keys, opt, src, old_date, new_date, min_oi):
    d1 = (df[(df["date"].dt.date == old_date) & (df["option_type"] == opt)]
          [["ric", src]].set_index("ric"))
    d2 = (df[(df["date"].dt.date == new_date) & (df["option_type"] == opt)]
          [["ric", src]].set_index("ric"))
    merged = d1.join(d2, how="outer", lsuffix="_1", rsuffix="_2")
    merged["val"] = (pd.to_numeric(merged[src + "_2"], errors="coerce")
                     - pd.to_numeric(merged[src + "_1"], errors="coerce"))
    v = _valid(df, opt, new_date, min_oi)
    if v is not None:
        merged = merged[merged.index.isin(v)]
    meta = _meta(df, opt)
    result = merged.join(meta[["strike", "mk"]]).dropna(subset=["strike"])
    result = result[result["mk"].notna()]
    piv = result.pivot_table(index="strike", columns="mk", values="val", aggfunc="first")
    return _clean(piv, month_keys).sort_index(ascending=False)

def get_oi_pivot(df, month_keys, opt, old_date, new_date, min_oi):
    return _change_pivot(df, month_keys, opt, "oi", old_date, new_date, min_oi)

def get_px_pivot(df, month_keys, opt, old_date, new_date, min_oi):
    return _change_pivot(df, month_keys, opt, "settle", old_date, new_date, min_oi)

@st.cache_data(ttl=1800)
def get_vol_pivot(df, month_keys, opt, old_date, new_date, min_oi):
    lo, hi = min(old_date, new_date), max(old_date, new_date)
    sub = df[(df["option_type"] == opt)
             & (df["date"].dt.date >= lo)
             & (df["date"].dt.date <= hi)].copy()
    v = _valid(df, opt, new_date, min_oi)
    if v is not None:
        sub = sub[sub["ric"].isin(v)]
    sub["mk"] = list(zip(sub["expiry_month"].astype(int), sub["expiry_year"].astype(int)))
    sub["volume"] = pd.to_numeric(sub["volume"], errors="coerce")
    piv = sub.groupby(["strike", "mk"])["volume"].sum().unstack("mk")
    return _clean(piv, month_keys).sort_index(ascending=False)

def get_pct_pivot(df, month_keys, opt, old_date, new_date, min_oi):
    d1 = (df[(df["date"].dt.date == old_date) & (df["option_type"] == opt)]
          [["ric", "settle"]].set_index("ric"))
    d2 = (df[(df["date"].dt.date == new_date) & (df["option_type"] == opt)]
          [["ric", "settle"]].set_index("ric"))
    merged = d1.join(d2, how="outer", lsuffix="_1", rsuffix="_2")
    s1 = pd.to_numeric(merged["settle_1"], errors="coerce")
    s2 = pd.to_numeric(merged["settle_2"], errors="coerce")
    mask = (s1.fillna(0).abs() > 0)
    merged["val"] = np.where(mask, ((s2 - s1) / s1.fillna(0).abs()) * 100, np.nan)
    v = _valid(df, opt, new_date, min_oi)
    if v is not None:
        merged = merged[merged.index.isin(v)]
    meta = _meta(df, opt)
    result = merged.join(meta[["strike", "mk"]]).dropna(subset=["strike"])
    result = result[result["mk"].notna()]
    piv = result.pivot_table(index="strike", columns="mk", values="val", aggfunc="first")
    return _clean(piv, month_keys).sort_index(ascending=False)

@st.cache_data(ttl=1800)
def get_iv_pivot(df, month_keys, opt, snap_date, min_oi):
    """Snapshot of ImpVol by strike × expiry on snap_date."""
    if "impvol" not in df.columns:
        return pd.DataFrame()
    d = (df[(df["date"].dt.date == snap_date) & (df["option_type"] == opt)]
         [["ric", "impvol"]].set_index("ric"))
    d["impvol"] = pd.to_numeric(d["impvol"], errors="coerce")
    meta = _meta(df, opt)
    result = d.join(meta[["strike", "mk"]]).dropna(subset=["strike", "impvol"])
    result = result[result["mk"].notna()]
    piv = result.pivot_table(index="strike", columns="mk", values="impvol", aggfunc="first")
    return _clean(piv, month_keys).sort_index(ascending=False)

@st.cache_data(ttl=1800)
def get_iv_change_pivot(df, month_keys, opt, old_date, new_date, min_oi):
    """ImpVol change (new − old) by strike × expiry."""
    if "impvol" not in df.columns:
        return pd.DataFrame()
    d1 = (df[(df["date"].dt.date == old_date) & (df["option_type"] == opt)]
          [["ric", "impvol"]].set_index("ric"))
    d2 = (df[(df["date"].dt.date == new_date) & (df["option_type"] == opt)]
          [["ric", "impvol"]].set_index("ric"))
    merged = d1.join(d2, how="outer", lsuffix="_1", rsuffix="_2")
    merged["val"] = (pd.to_numeric(merged["impvol_2"], errors="coerce")
                     - pd.to_numeric(merged["impvol_1"], errors="coerce"))
    meta = _meta(df, opt)
    result = merged.join(meta[["strike", "mk"]]).dropna(subset=["strike"])
    result = result[result["mk"].notna()]
    piv = result.pivot_table(index="strike", columns="mk", values="val", aggfunc="first")
    return _clean(piv, month_keys).sort_index(ascending=False)

@st.cache_data(ttl=1800)
def get_oi_snapshot_pivot(df, month_keys, opt, snap_date, new_date, min_oi):
    d = (df[(df["date"].dt.date == snap_date) & (df["option_type"] == opt)]
         [["ric", "oi"]].set_index("ric"))
    d = d.copy()
    d["oi"] = pd.to_numeric(d["oi"], errors="coerce")
    v = _valid(df, opt, new_date, min_oi)
    if v is not None:
        d = d[d.index.isin(v)]
    meta = _meta(df, opt)
    result = d.join(meta[["strike", "mk"]]).dropna(subset=["strike"])
    result = result[result["mk"].notna()]
    piv = result.pivot_table(index="strike", columns="mk", values="oi", aggfunc="first")
    return _clean(piv, month_keys).sort_index(ascending=False)


# ── Colors ─────────────────────────────────────────────────────────────────────
def _alpha(v, mx): return round(0.15 + min(abs(float(v)) / max(mx, 0.01), 1.0) * 0.50, 2)

def oi_color(val, mx):
    if pd.isna(val) or val == 0: return ""
    a = _alpha(val, mx)
    return (f"background:rgba(66,133,244,{a});color:#1a1a2e" if val > 0
            else f"background:rgba(220,75,75,{a});color:#1a1a2e")

def vol_color(val, mx):
    if pd.isna(val) or val == 0: return ""
    a = _alpha(val, mx)
    return f"background:rgba(66,133,244,{a});color:#1a1a2e"

def px_color(val, mx):
    if pd.isna(val) or val == 0: return ""
    a = _alpha(val, mx)
    return (f"background:rgba(52,168,83,{a});color:#1a1a2e" if val > 0
            else f"background:rgba(220,75,75,{a});color:#1a1a2e")

def iv_color(val, mx):
    """ImpVol level — heat map: low=blue, high=orange."""
    if pd.isna(val) or val == 0: return ""
    a = round(0.15 + min(float(val) / max(mx, 0.01), 1.0) * 0.65, 2)
    return f"background:rgba(234,88,12,{a});color:#1a1a2e"

def iv_chg_color(val, mx):
    """IV change — green=vol fell, red=vol rose."""
    if pd.isna(val) or val == 0: return ""
    a = _alpha(val, mx)
    return (f"background:rgba(220,75,75,{a});color:#1a1a2e" if val > 0
            else f"background:rgba(52,168,83,{a});color:#1a1a2e")


# ── Butterfly HTML ─────────────────────────────────────────────────────────────
_CSS = """<style>
.bft{border-collapse:collapse;font-size:11px;font-family:-apple-system,sans-serif}
.bft th,.bft td{white-space:nowrap;padding:2px 5px}
.bft th{font-weight:600;letter-spacing:.03em;font-size:10px;text-align:center}
.bft td{text-align:right;border:1px solid #f0f0f0;color:#1a1a2e}
.bft .sc{text-align:center;font-weight:700;font-size:11px;color:#1a1a2e;
         background:#f5f5f5;border-left:2px solid #ccc;border-right:2px solid #ccc}
.bft .sc-atm{background:#f59e0b!important;color:#1a1a2e!important;font-weight:900!important}
.bft tr.atm-row td{border-top:2px solid #f59e0b!important;border-bottom:2px solid #f59e0b!important}
.bft tfoot td{font-weight:700;border-top:2px solid #bbb}
.bft tfoot .sc{font-size:9px;color:#888;background:#efefef}
.ch{background:#dce8fb;color:#1a56cc}
.ph{background:#fde8e8;color:#c0392b}
.kch{background:#ebebeb;color:#555}
</style>"""


def butterfly_html(cpiv, ppiv, atm, cfn, month_keys, fmt="{:.0f}",
                   footer=True, sfx="", title="", atm_tol=None, fixed_strikes=None,
                   snap_tol=None):
    ccols = list(reversed(month_keys))
    pcols = list(month_keys)

    if fixed_strikes is not None:
        strikes = list(fixed_strikes)  # caller controls order (asc = low at top, ATM centered)
    else:
        strikes_set = set()
        if not cpiv.empty: strikes_set.update(cpiv.index.tolist())
        if not ppiv.empty: strikes_set.update(ppiv.index.tolist())
        strikes = sorted(strikes_set)  # low to high

    if atm_tol is None:
        if len(strikes) >= 2:
            gaps = [abs(strikes[i] - strikes[i+1]) for i in range(len(strikes)-1)]
            atm_tol = min(gaps) * 0.6
        else:
            atm_tol = 1.0

    def _flat(p):
        if p.empty: return np.array([], dtype=float)
        return p.values.astype(float).flatten()

    av = np.concatenate([_flat(cpiv), _flat(ppiv)])
    av = av[~np.isnan(av)]
    mx = float(np.max(np.abs(av))) if len(av) > 0 else 1.0

    nc, np_ = len(ccols), len(pcols)

    h1 = (f'<tr><th colspan="{nc}" class="ch">Call</th>'
          f'<th class="kch">{title}</th>'
          f'<th colspan="{np_}" class="ph">Put</th></tr>')

    h2 = ('<tr>'
          + "".join(f'<th class="ch" style="color:#999;font-weight:400">'
                    f'{CALL_CODES[m]}{str(y)[-2:]}</th>' for m, y in ccols)
          + '<th class="kch"></th>'
          + "".join(f'<th class="ph" style="color:#ccc;font-weight:400">'
                    f'{PUT_CODES[m]}{str(y)[-2:]}</th>' for m, y in pcols)
          + '</tr>')

    h3 = ('<tr>'
          + "".join(f'<th class="ch">{MONTH_NAMES[m]}</th>' for m, y in ccols)
          + '<th class="kch"></th>'
          + "".join(f'<th class="ph">{MONTH_NAMES[m]}</th>' for m, y in pcols)
          + '</tr>')

    _piv_idx_cache = {}
    def cv(piv, s, mk):
        if piv.empty or mk not in piv.columns: return np.nan
        # nearest-key lookup within snap_tol (tolerates display grid ≠ data grid)
        if snap_tol is not None:
            pid = id(piv)
            if pid not in _piv_idx_cache:
                _piv_idx_cache[pid] = np.array(piv.index.tolist(), dtype=float)
            idx_arr = _piv_idx_cache[pid]
            if len(idx_arr) == 0: return np.nan
            diffs = np.abs(idx_arr - s)
            if diffs.min() > snap_tol: return np.nan
            s = idx_arr[diffs.argmin()]
        elif s not in piv.index:
            return np.nan
        v = piv.at[s, mk]
        return float(v) if not pd.isna(v) else np.nan

    def td(v):
        style = cfn(v, mx)
        txt = (fmt.format(v) + sfx) if not np.isnan(v) and v != 0 else ""
        return f'<td style="{style}">{txt}</td>'

    body = []
    for s in strikes:
        is_atm = atm is not None and abs(s - atm) < atm_tol
        sc     = "sc sc-atm" if is_atm else "sc"
        tr_cls = ' class="atm-row"' if is_atm else ""
        lbl    = int(s) if s == int(s) else s
        row = ("".join(td(cv(cpiv, s, mk)) for mk in ccols)
               + f'<td class="{sc}">{lbl}</td>'
               + "".join(td(cv(ppiv, s, mk)) for mk in pcols))
        body.append(f"<tr{tr_cls}>{row}</tr>")

    ft = ""
    if footer:
        def cs(piv, mk):
            if piv.empty or mk not in piv.columns or piv[mk].notna().sum() == 0:
                return float("nan")
            return float(piv[mk].sum(skipna=True))
        cft = "".join(td(cs(cpiv, mk)) for mk in ccols)
        pft = "".join(td(cs(ppiv, mk)) for mk in pcols)
        ft = (f'<tfoot><tr>{cft}'
              f'<td class="sc" style="font-size:9px;color:#888">TOT</td>'
              f'{pft}</tr></tfoot>')

    est_h = max(400, (len(strikes) + 4) * 22 + 90)
    return (f'{_CSS}<div style="overflow-x:auto;overflow-y:auto;max-height:{est_h}px">'
            f'<table class="bft"><thead>{h1}{h2}{h3}</thead>'
            f'{ft}<tbody>{"".join(body)}</tbody></table></div>')


# ── Misc helpers ───────────────────────────────────────────────────────────────
def _tot(piv):
    # NaN, not 0.0, when the pivot has genuinely no data (e.g. OI is null
    # across the board on the very latest date — LSEG publishes OI a day
    # behind Settle/Volume). piv.sum(skipna=True) alone silently turns an
    # all-NaN column into 0, which read as "no positioning change" in the
    # KPI row when the real answer is "no data yet for today".
    if piv.empty or piv.notna().to_numpy().sum() == 0:
        return float("nan")
    return float(piv.sum(skipna=True).sum())

def _fn(v, f="{:,.0f}"):
    try:
        v = float(v)
        if pd.isna(v):
            return "—"
        return f.format(v)
    except Exception:
        return "—"

# RIC reconstruction — LSEG scheme (interim migration), NOT the ICE
# "<ROOT> <month><yy><C/P><strike>" scheme these were originally written
# for. LSEG RICs are "1<ROOT><strike_encoded><month_code><yy>", with
# A-L = Jan-Dec calls and M-X = Jan-Dec puts (see Code/_common.py /
# Code/kc_ingest_lseg.py). Left as the ICE-style builders, this lookup
# silently never matched our data's "ric" column, so every row's time
# series panel showed "No data" — that's the bug being fixed here.
def _ric_kc(strike, month, year, opt):
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"1KC{int(round(strike * 100))}{code}{yy}"

def _ric_cc(strike, month, year, opt):
    """CC strikes are stored as whole $/mt already — no conversion needed."""
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"1CC{int(round(strike))}{code}{yy}"

def _ric_sb(strike, month, year, opt):
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"1SB{int(round(strike * 100))}{code}{yy}"

def _ric_ct(strike, month, year, opt):
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"1CT{int(round(strike * 100))}{code}{yy}"

def _ric_lrc(strike, month, year, opt):
    """LRC (Robusta) — no leading '1' (already an unambiguous root),
    raw $/tonne strikes, no *100 encoding."""
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"LRC{int(round(strike))}{code}{yy}"

def _ric_lcc(strike, month, year, opt):
    """LCC (London Cocoa) — no leading '1', raw strike scale, no *100 encoding."""
    code = CALL_CODES[month] if opt == "Call" else PUT_CODES[month]
    yy   = f"{year % 100:02d}"
    return f"LCC{int(round(strike))}{code}{yy}"


COMMODITIES = [
    dict(key="KC",  title="KC",  tab_label="Arabica",     ric_fn=_ric_kc,
         display_step=2.5, mround_default=50,
         ingest_note="MRound=50 ¢/lb for ATM snap | Step=2.5 ¢/lb (kc_ingest_lseg.py STRIKE_GAP)",
         fut_name="kc",
         atm_fmt=lambda v: f"{int(v) if v == int(v) else v}"),
    dict(key="LRC", title="LRC", tab_label="Robusta",      ric_fn=_ric_lrc,
         display_step=25, mround_default=25,
         ingest_note="MRound=25 $/tonne for ATM snap | Step=25 $/tonne | "
                      "active months Jan/Mar/May/Jul/Sep/Nov only (confirmed live vs LSEG)",
         fut_name="lrc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="CC",  title="CC",  tab_label="NYC Cocoa",    ric_fn=_ric_cc,
         display_step=50, mround_default=300,
         ingest_note="MRound=300 $/mt for ATM snap | Step=50 $/mt (cc_ingest_lseg.py STRIKE_GAP)",
         fut_name="cc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="LCC", title="LCC", tab_label="London Cocoa", ric_fn=_ric_lcc,
         display_step=25, mround_default=25,
         ingest_note="MRound=25 for ATM snap | Step=25 | "
                      "active months Mar/May/Jul/Sep/Dec only (confirmed live vs LSEG)",
         fut_name="lcc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="SB",  title="SB",  tab_label="Sugar (SB)",   ric_fn=_ric_sb,
         display_step=0.25, mround_default=0.25,
         ingest_note="MRound=0.25 cts/lb for ATM snap | Step=0.25 cts/lb (sb_ingest_lseg.py STRIKE_GAP)",
         fut_name="sb",
         atm_fmt=lambda v: f"{v:.2f}"),
    dict(key="CT",  title="CT",  tab_label="Cotton",       ric_fn=_ric_ct,
         display_step=1, mround_default=1,
         ingest_note="MRound=1 cts/lb for ATM snap | Step=1 cts/lb (ct_ingest_lseg.py STRIKE_GAP)",
         fut_name="ct",
         atm_fmt=lambda v: f"{int(v)}"),
]


def render_controls(df, atm_val, atm_label, atm_data, key_prefix, title,
                    display_step=None, mround_default=None, ingest_note=""):
    """Renders the shared Controls expander (Min OI / Price / MRound / Step / Mode).
    Returns (min_oi, custom_atm, custom_step, strike_mode, month_keys, all_strikes_data)."""
    month_keys       = _month_keys(df)
    all_strikes_data = sorted(df["strike"].unique())  # ascending, for step inference
    atm_updated      = atm_data.get("updated", "—")

    if atm_val is not None and len(all_strikes_data) > 1:
        if display_step is not None:
            step = display_step
        else:
            diffs = [all_strikes_data[i+1] - all_strikes_data[i]
                     for i in range(len(all_strikes_data)-1)]
            step = sorted(diffs)[len(diffs)//2]
    else:
        step = 1.0

    _def_step   = float(display_step if display_step else (step if atm_val is not None and len(all_strikes_data) > 1 else 1.0))
    _def_mround = float(mround_default if mround_default is not None else _def_step)

    with st.expander("Controls", expanded=False):
        col_oi, col_price, col_mround, col_step, col_mode = st.columns([1, 1.2, 0.8, 0.8, 1.4])
        with col_oi:
            min_oi = st.number_input("Min OI filter (New Date)", value=0, min_value=0,
                                      step=10, key=f"{key_prefix}_min_oi",
                                      help="Hide strikes where Open Interest on the New Date is below this threshold.")
        with col_price:
            raw_price = st.number_input(
                "Price", value=float(atm_val) if atm_val is not None else 0.0,
                format="%.2f", key=f"{key_prefix}_raw_price",
                help="Raw market price (e.g. last futures settle). The table centers on MROUND(Price, MRound)."
            )
        with col_mround:
            mround_val = st.number_input(
                "MRound", value=_def_mround, min_value=0.01,
                format="%.2f", key=f"{key_prefix}_mround",
                help=(
                    "Rounding multiple for the ATM. Center ATM = nearest multiple of this value to Price "
                    "(e.g. Price=302.5, MRound=50 → ATM=300).\n\n"
                    + (f"Ingest uses: {ingest_note}" if ingest_note else "")
                )
            )
        with col_step:
            custom_step = st.number_input(
                "Step", value=_def_step, min_value=0.01,
                format="%.2f", key=f"{key_prefix}_custom_step",
                help=(
                    "Strike ladder increment — gap between rows in the table.\n\n"
                    + (f"Ingest uses: {ingest_note}" if ingest_note else "")
                )
            )
        with col_mode:
            strike_mode = st.radio(
                "Strike mode", ["Exact", "Nearest"],
                index=0, horizontal=True,
                key=f"{key_prefix}_strike_mode",
                help=(
                    "Nearest: grid rows at exact step intervals, data pulled from the "
                    "closest parquet strike within Step/2 — clean uniform ladder.\n\n"
                    "Exact: rows are the actual strikes from the parquet, centered "
                    "on ATM — no interpolation, raw exchange data only."
                )
            )

        custom_atm = round(raw_price / mround_val) * mround_val if mround_val > 0 else raw_price
        st.caption(
            f"Center ATM: **{custom_atm:,.2f}** = MROUND({raw_price:,.2f}, {mround_val:,.2f})  |  "
            f"ATM ({title}): **{atm_label}** as of {atm_updated}  |  "
            f"Data: {df['date'].min().date()} to {df['date'].max().date()}"
        )

    return min_oi, custom_atm, custom_step, strike_mode, month_keys, all_strikes_data


def build_strike_grid(custom_atm, custom_step, strike_mode, all_strikes_data, N=35):
    """Returns (all_strikes, snap_tol) for the display grid."""
    if strike_mode == "Nearest":
        all_strikes = [round(custom_atm + i * custom_step, 6)
                       for i in range(-N, N+1)
                       if custom_atm + i * custom_step > 0]
        snap_tol = custom_step / 2
    else:
        snap = {}
        for s in all_strikes_data:
            bucket = round((s - custom_atm) / custom_step)
            if bucket not in snap or abs(s - custom_atm) < abs(snap[bucket] - custom_atm):
                snap[bucket] = s
        all_strikes = sorted([snap[b] for b in range(-N, N+1) if b in snap])
        snap_tol = None

    return all_strikes, snap_tol
