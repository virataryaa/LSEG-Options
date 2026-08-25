"""
common.py — Shared data loaders, pivot helpers, and rendering utilities
for the Options Dashboard apps.

  - app.py                   → OI Change + Volume (fast, default view)
  - oi_advanced_analytics.py → Px Change, Vol Surface, IV vs RV

Each app runs as its own Streamlit process, so @st.cache_data caches are
NOT shared between them — each app only loads/computes what it renders.

Strike-grid design (see build_strike_grid / project_to_grid)
-----------------------------------------------------------
The butterfly tables show a ladder of strike rows centered on the ATM.
Two modes, with deliberately different contracts:

  Exact   — rows ARE the real strikes from the parquet, N each side of the
            strike nearest ATM. No arithmetic grid, no bucketing, so no
            strike is ever dropped or merged. "Step" does not apply here
            and is disabled in the UI.

  Nearest — a uniform arithmetic ladder at "Step" intervals, clamped to the
            traded strike range, with each data strike snapped to its own
            nearest row (within Step/2).

Both paths go through project_to_grid(), which maps *data strike -> display
row*. Because that is a function (each source strike has exactly one nearest
row), a strike can never be counted on two rows, and rows are filled by
aggregating collisions rather than silently dropping them. The footer total
is then computed from the projected frame, so TOT always reconciles with the
rows actually on screen.
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

# Distinct series colors for multi-select drill-down charts.
SERIES_COLORS = ["#4285f4", "#dc4b4b", "#f59e0b", "#34a853",
                 "#8b5cf6", "#06b6d4", "#f97316", "#ec4899"]


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


# ── OI availability ────────────────────────────────────────────────────────────
# LSEG publishes Open Interest a full session behind Settle/Volume, so the most
# recent date in the parquet routinely has *zero* non-null OI for every strike.
# Defaulting "New Date" to that date produced a completely blank OI Change table
# and — because the Min OI filter keys off OI on New Date — silently blanked the
# Volume table too the moment a filter was set. These helpers let the UI default
# to, and fall back to, the latest date that actually carries OI.
@st.cache_data(ttl=1800)
def oi_dates(df: pd.DataFrame):
    """Sorted list of dates where at least one strike has non-null OI."""
    if df.empty or "oi" not in df.columns:
        return []
    g = df.groupby(df["date"].dt.date)["oi"].apply(lambda s: s.notna().any())
    return sorted([d for d, ok in g.items() if ok])


def latest_oi_date(df: pd.DataFrame, on_or_before=None):
    """Most recent date carrying OI, optionally at or before a cutoff."""
    ds = oi_dates(df)
    if not ds:
        return None
    if on_or_before is not None:
        ds = [d for d in ds if d <= on_or_before]
    return ds[-1] if ds else None


def render_sidebar(dfs, title="Options Dashboard"):
    """Shared Old Date / New Date picker + latest-data status. Returns (old_date, new_date)."""
    all_dates = set()
    for _df in dfs.values():
        if not _df.empty:
            all_dates.update(_df["date"].dt.date.unique())
    available_dates = sorted(all_dates)

    # Default New Date to the newest date that has OI somewhere, so the app
    # lands on a view that actually renders instead of an all-blank table.
    oi_any = set()
    for _df in dfs.values():
        oi_any.update(oi_dates(_df))
    default_new = max(oi_any) if oi_any else (available_dates[-1] if available_dates else None)
    default_new_idx = (available_dates.index(default_new)
                       if default_new in available_dates else len(available_dates) - 1)

    with st.sidebar:
        st.title(title)
        st.divider()
        old_date = st.selectbox("Old Date", available_dates,
                                 index=max(0, default_new_idx - 9),
                                 format_func=lambda d: d.strftime("%d %b %Y"))
        new_date = st.selectbox("New Date", available_dates,
                                 index=max(0, default_new_idx),
                                 format_func=lambda d: d.strftime("%d %b %Y"))
        if old_date == new_date:
            st.warning("Old Date and New Date are the same.")
        elif old_date > new_date:
            st.warning("Old Date is after New Date — changes will be signed backwards.")

        st.divider()
        st.markdown("**Latest data available**")
        for _label, _key in [("Arabica (KC)", "KC"), ("Robusta (LRC)", "LRC"),
                             ("NYC Cocoa (CC)", "CC"), ("London Cocoa (LCC)", "LCC"),
                             ("Sugar (SB)", "SB"), ("Cotton (CT)", "CT")]:
            _df = dfs[_key]
            if not _df.empty:
                _latest = _df["date"].max().date()
                _oi = latest_oi_date(_df)
                note = "" if _oi == _latest else f" (OI to {_oi.strftime('%d %b')})" if _oi else " (no OI)"
                st.caption(f"{_label} — {_latest.strftime('%d %b %Y')}{note}")
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
    """RICs passing the Min OI filter on New Date.

    Falls back to the most recent prior date that carries OI when New Date has
    none — otherwise a filter of any size would return an empty set and blank
    out Volume/Px tables that have perfectly good data.
    """
    if min_oi <= 0:
        return None
    eff = new_date
    day = df[(df["date"].dt.date == new_date) & (df["option_type"] == opt)]
    if day.empty or not day["oi"].notna().any():
        fb = latest_oi_date(df, on_or_before=new_date)
        if fb is None:
            return None  # no OI anywhere — don't filter rather than blank everything
        eff = fb
    d2 = df[(df["date"].dt.date == eff) & (df["option_type"] == opt)][["ric", "oi"]]
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
    # min_count=1 so a strike/expiry with no reported volume at all stays NaN
    # (blank) instead of collapsing to a misleading 0.
    piv = sub.groupby(["strike", "mk"])["volume"].sum(min_count=1).unstack("mk")
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


# ── Strike grid ────────────────────────────────────────────────────────────────
def mround(value, multiple):
    """Excel MROUND semantics — round half AWAY from zero.

    Python's built-in round() is banker's rounding, which made the ATM centre
    jump inconsistently: MROUND(375,50) gave 400 but MROUND(425,50) also gave
    400, because 7.5 rounds up to 8 while 8.5 rounds down to 8.
    """
    if multiple is None or multiple <= 0:
        return float(value)
    q = float(value) / float(multiple)
    return float(np.floor(q + 0.5) if q >= 0 else np.ceil(q - 0.5)) * float(multiple)


def build_strike_grid(custom_atm, custom_step, strike_mode, all_strikes_data, n_side=25):
    """Display strike rows, ascending.

    Returns (rows, snap_tol):
      Exact   -> real parquet strikes, n_side each side of the strike nearest
                 ATM. snap_tol is None (exact index match). Step is ignored.
      Nearest -> uniform ladder at custom_step, clamped to the traded strike
                 range so we never render dozens of blank rows off the end of
                 the board. snap_tol = step/2.
    """
    data = sorted(float(s) for s in all_strikes_data)
    if not data:
        return [], None

    if strike_mode == "Exact":
        arr = np.asarray(data, dtype=float)
        j = int(np.abs(arr - custom_atm).argmin())
        lo = max(0, j - n_side)
        hi = min(len(arr), j + n_side + 1)
        return [float(x) for x in arr[lo:hi]], None

    step = float(custom_step) if custom_step and custom_step > 0 else 1.0
    lo_d, hi_d = data[0], data[-1]
    rows = []
    for i in range(-n_side, n_side + 1):
        r = round(custom_atm + i * step, 6)
        # Clamp to the traded range (half a step of tolerance at each end) so
        # the ladder covers the board rather than empty space beyond it.
        if r > 0 and (lo_d - step / 2) <= r <= (hi_d + step / 2):
            rows.append(r)
    return rows, step / 2


def project_to_grid(piv, rows, snap_tol, how="sum"):
    """Reindex a strike-indexed pivot onto the display rows.

    Maps *each data strike to its single nearest display row*, which makes
    double-counting structurally impossible (the mapping is a function). When
    several strikes land on one row they are combined per `how` rather than
    silently dropped:
        how="sum"     — additive quantities (OI, Volume)
        how="nearest" — level quantities (price, %, IV): take the closest strike
    """
    rows = [float(r) for r in rows]
    empty = pd.DataFrame(index=pd.Index(rows, name="strike"),
                         columns=list(piv.columns) if piv is not None and not piv.empty else [],
                         dtype=float)
    if piv is None or piv.empty or not rows:
        return empty

    if snap_tol is None:
        out = piv.reindex(rows)
        out.index = pd.Index(rows, name="strike")
        return out.astype(float)

    src = np.asarray(piv.index, dtype=float)
    tgt = np.asarray(rows, dtype=float)
    nearest = np.abs(src[:, None] - tgt[None, :]).argmin(axis=1)
    dist = np.abs(src - tgt[nearest])
    keep = dist <= (snap_tol + 1e-9)
    if not keep.any():
        return empty

    vals = piv.loc[keep].astype(float).copy()
    grp = pd.Series(tgt[nearest[keep]], index=vals.index, name="__row")

    if how == "sum":
        out = vals.groupby(grp).sum(min_count=1)
    else:
        order = np.argsort(dist[keep], kind="stable")
        vals = vals.iloc[order]
        out = vals.groupby(grp.iloc[order]).first()

    out = out.reindex(rows)
    out.index = pd.Index(rows, name="strike")
    return out.astype(float)


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
                   footer=True, sfx="", title="", strikes=None):
    """Pure renderer — `cpiv`/`ppiv` must already be projected onto `strikes`
    via project_to_grid(). No snapping happens here, so the footer total is
    computed from exactly the cells on screen and always reconciles with them.
    """
    ccols = list(reversed(month_keys))
    pcols = list(month_keys)

    if strikes is None:
        s_set = set()
        if cpiv is not None and not cpiv.empty: s_set.update(cpiv.index.tolist())
        if ppiv is not None and not ppiv.empty: s_set.update(ppiv.index.tolist())
        strikes = sorted(s_set)
    strikes = [float(s) for s in strikes]

    if not strikes:
        return ('<div style="padding:14px;color:#888;font-size:12px;'
                'border:1px dashed #ddd;border-radius:4px">No strikes to display.</div>')

    def _flat(p):
        if p is None or p.empty: return np.array([], dtype=float)
        return p.to_numpy(dtype=float).flatten()

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

    # Exactly one ATM row: the display row closest to the centre price. A
    # tolerance test could match zero rows (ATM between strikes) or several.
    atm_row = None
    if atm is not None:
        atm_row = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))

    def col_vals(piv, mk):
        if piv is None or piv.empty or mk not in piv.columns:
            return np.full(len(strikes), np.nan)
        return piv[mk].to_numpy(dtype=float)

    cmat = {mk: col_vals(cpiv, mk) for mk in ccols}
    pmat = {mk: col_vals(ppiv, mk) for mk in pcols}

    def td(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return '<td></td>'
        style = cfn(v, mx)
        txt = (fmt.format(v) + sfx) if v != 0 else ""
        return f'<td style="{style}">{txt}</td>'

    body = []
    for i, s in enumerate(strikes):
        is_atm = (i == atm_row)
        sc     = "sc sc-atm" if is_atm else "sc"
        tr_cls = ' class="atm-row"' if is_atm else ""
        lbl    = int(s) if float(s).is_integer() else round(s, 4)
        row = ("".join(td(cmat[mk][i]) for mk in ccols)
               + f'<td class="{sc}">{lbl}</td>'
               + "".join(td(pmat[mk][i]) for mk in pcols))
        body.append(f"<tr{tr_cls}>{row}</tr>")

    ft = ""
    if footer:
        def cs(mat, mk):
            col = mat[mk]
            if np.all(np.isnan(col)):
                return None
            return float(np.nansum(col))
        cft = "".join(td(cs(cmat, mk)) for mk in ccols)
        pft = "".join(td(cs(pmat, mk)) for mk in pcols)
        ft = (f'<tfoot><tr>{cft}'
              f'<td class="sc" style="font-size:9px;color:#888">TOT</td>'
              f'{pft}</tr></tfoot>')

    est_h = max(400, (len(strikes) + 4) * 22 + 90)
    return (f'{_CSS}<div style="overflow-x:auto;overflow-y:auto;max-height:{est_h}px">'
            f'<table class="bft"><thead>{h1}{h2}{h3}</thead>'
            f'{ft}<tbody>{"".join(body)}</tbody></table></div>')


def render_butterfly(cpiv, ppiv, grid, atm, cfn, month_keys, how="sum", **kw):
    """project_to_grid + butterfly_html in one call. `grid` is (rows, snap_tol)."""
    rows, tol = grid
    cp = project_to_grid(cpiv, rows, tol, how=how)
    pp = project_to_grid(ppiv, rows, tol, how=how)
    return butterfly_html(cp, pp, atm, cfn, month_keys, strikes=rows, **kw)


# ── Misc helpers ───────────────────────────────────────────────────────────────
def _tot(piv):
    # NaN, not 0.0, when the pivot has genuinely no data (e.g. OI is null
    # across the board on the very latest date — LSEG publishes OI a day
    # behind Settle/Volume). piv.sum(skipna=True) alone silently turns an
    # all-NaN column into 0, which read as "no positioning change" in the
    # KPI row when the real answer is "no data yet for today".
    if piv is None or piv.empty or piv.notna().to_numpy().sum() == 0:
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

def fmt_strike(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{int(x)}" if x.is_integer() else f"{x:g}"

# RIC reconstruction — LSEG scheme (interim migration), NOT the ICE
# "<ROOT> <month><yy><C/P><strike>" scheme these were originally written
# for. LSEG RICs are "1<ROOT><strike_encoded><month_code><yy>", with
# A-L = Jan-Dec calls and M-X = Jan-Dec puts (see Code/_common.py /
# Code/kc_ingest_lseg.py). Verified 100% against every RIC in all six
# parquets.
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


# `mround_default` is the *display* centring multiple. It defaults to the
# strike gap so the highlighted ATM row lands on a real strike: the previous
# values (KC 50, CC 300) were the coarse ingest snap, which centred KC at 400
# when the ATM was 375 and CC at 5700 when the ATM was 5750. The ingest snap
# is still surfaced in the control's help text via `ingest_note`.
COMMODITIES = [
    dict(key="KC",  title="KC",  tab_label="Arabica",     ric_fn=_ric_kc,
         display_step=2.5, mround_default=2.5,
         ingest_note="Ingest ATM snap MRound=50 ¢/lb | Step=2.5 ¢/lb (kc_ingest_lseg.py STRIKE_GAP)",
         fut_name="kc",
         atm_fmt=lambda v: f"{int(v) if float(v).is_integer() else v}"),
    dict(key="LRC", title="LRC", tab_label="Robusta",      ric_fn=_ric_lrc,
         display_step=25, mround_default=25,
         ingest_note="Ingest ATM snap MRound=25 $/tonne | Step=25 $/tonne | "
                      "active months Jan/Mar/May/Jul/Sep/Nov only (confirmed live vs LSEG)",
         fut_name="lrc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="CC",  title="CC",  tab_label="NYC Cocoa",    ric_fn=_ric_cc,
         display_step=50, mround_default=50,
         ingest_note="Ingest ATM snap MRound=300 $/mt | Step=50 $/mt (cc_ingest_lseg.py STRIKE_GAP)",
         fut_name="cc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="LCC", title="LCC", tab_label="London Cocoa", ric_fn=_ric_lcc,
         display_step=25, mround_default=25,
         ingest_note="Ingest ATM snap MRound=25 | Step=25 | "
                      "active months Mar/May/Jul/Sep/Dec only (confirmed live vs LSEG)",
         fut_name="lcc",
         atm_fmt=lambda v: f"{int(v):,}"),
    dict(key="SB",  title="SB",  tab_label="Sugar (SB)",   ric_fn=_ric_sb,
         display_step=0.25, mround_default=0.25,
         ingest_note="Ingest ATM snap MRound=0.25 cts/lb | Step=0.25 cts/lb (sb_ingest_lseg.py STRIKE_GAP)",
         fut_name="sb",
         atm_fmt=lambda v: f"{v:.2f}"),
    dict(key="CT",  title="CT",  tab_label="Cotton",       ric_fn=_ric_ct,
         display_step=1, mround_default=1,
         ingest_note="Ingest ATM snap MRound=1 cts/lb | Step=1 cts/lb (ct_ingest_lseg.py STRIKE_GAP)",
         fut_name="ct",
         atm_fmt=lambda v: f"{int(v)}"),
]


def oi_notice(df, new_date, key):
    """Warn when the chosen New Date carries no OI (LSEG publishes it a day late)."""
    day = df[df["date"].dt.date == new_date]
    if day.empty or day["oi"].notna().any():
        return
    fb = latest_oi_date(df, on_or_before=new_date)
    if fb:
        st.warning(
            f"No Open Interest published for {key} on {new_date.strftime('%d %b %Y')} "
            f"— LSEG releases OI one session behind Settle/Volume. "
            f"OI Change will be blank; latest OI is **{fb.strftime('%d %b %Y')}**. "
            f"Pick that as New Date to see positioning. Volume and price are unaffected."
        )
    else:
        st.warning(f"No Open Interest data available for {key}.")


def render_controls(df, atm_val, atm_label, atm_data, key_prefix, title,
                    display_step=None, mround_default=None, ingest_note=""):
    """Renders the shared Controls expander. Returns a dict of resolved settings."""
    month_keys       = _month_keys(df)
    all_strikes_data = sorted(df["strike"].unique())
    atm_updated      = atm_data.get("updated", "—")

    # Median traded gap — the honest default step for this board.
    if len(all_strikes_data) > 1:
        diffs = np.diff(np.asarray(all_strikes_data, dtype=float))
        native_gap = float(np.median(diffs))
    else:
        native_gap = 1.0

    _def_step   = float(display_step if display_step else native_gap)
    _def_mround = float(mround_default if mround_default is not None else _def_step)

    with st.expander("Controls", expanded=False):
        c_oi, c_price, c_mround, c_mode, c_step, c_rows = st.columns([1, 1.1, 0.8, 1.2, 0.8, 0.8])

        with c_oi:
            min_oi = st.number_input(
                "Min OI filter (New Date)", value=0, min_value=0, step=10,
                key=f"{key_prefix}_min_oi",
                help="Hide strikes whose Open Interest on the New Date is below this. "
                     "If OI is not yet published for the New Date, the most recent "
                     "date that has OI is used instead.")
        with c_price:
            raw_price = st.number_input(
                "Price", value=float(atm_val) if atm_val is not None else 0.0,
                format="%.2f", key=f"{key_prefix}_raw_price",
                help="Raw market price (e.g. last futures settle). "
                     "The table centres on MROUND(Price, MRound).")
        with c_mround:
            mround_val = st.number_input(
                "MRound", value=_def_mround, min_value=0.0001, format="%.4f",
                key=f"{key_prefix}_mround",
                help="Centring multiple for the ATM row: ATM = nearest multiple of "
                     "this to Price (Excel MROUND, half away from zero). Defaults to "
                     "the strike gap so the ATM lands on a real strike.\n\n"
                     + (ingest_note or ""))
        with c_mode:
            strike_mode = st.radio(
                "Strike mode", ["Exact", "Nearest"], index=0, horizontal=True,
                key=f"{key_prefix}_strike_mode",
                help="Exact: rows are the real traded strikes from the parquet, "
                     "centred on ATM. Nothing is interpolated or merged, and Step "
                     "does not apply.\n\n"
                     "Nearest: a uniform ladder at Step intervals, clamped to the "
                     "traded range, with each strike snapped to its nearest row "
                     "(within Step/2).")
        with c_step:
            step_disabled = (strike_mode == "Exact")
            custom_step = st.number_input(
                "Step", value=_def_step, min_value=0.0001, format="%.4f",
                key=f"{key_prefix}_custom_step", disabled=step_disabled,
                help=("Not used in Exact mode — rows come straight from the traded "
                      "strikes. Switch to Nearest to build a uniform ladder."
                      if step_disabled else
                      f"Ladder increment between rows. Traded gap for {title} is "
                      f"{fmt_strike(native_gap)}."))
        with c_rows:
            n_side = st.number_input(
                "Rows ±", value=25, min_value=3, max_value=80, step=1,
                key=f"{key_prefix}_n_side",
                help="How many strike rows to show either side of the ATM.")

        custom_atm = mround(raw_price, mround_val)

        rows, snap_tol = build_strike_grid(custom_atm, custom_step, strike_mode,
                                           all_strikes_data, n_side=int(n_side))

        bits = [
            f"Centre ATM: **{custom_atm:,.4g}** = MROUND({raw_price:,.4g}, {mround_val:,.4g})",
            f"ATM ({title}): **{atm_label}** as of {atm_updated}",
            f"Rows: **{len(rows)}**"
            + (f" (traded strikes {fmt_strike(rows[0])}–{fmt_strike(rows[-1])})" if rows else ""),
            f"Traded gap: **{fmt_strike(native_gap)}**",
            f"Data: {df['date'].min().date()} to {df['date'].max().date()}",
        ]
        st.caption("  |  ".join(bits))
        if strike_mode == "Exact":
            st.caption("Exact mode — Step is disabled; every row is a real traded strike.")
        elif abs(custom_step - native_gap) > 1e-9:
            st.caption(
                f"Step {fmt_strike(custom_step)} differs from the traded gap "
                f"{fmt_strike(native_gap)} — strikes landing on the same row are "
                f"combined (summed for OI/Volume) so nothing is dropped, but rows "
                f"no longer map 1:1 to exchange strikes.")

    return dict(min_oi=int(min_oi), custom_atm=custom_atm, custom_step=float(custom_step),
                strike_mode=strike_mode, month_keys=month_keys,
                all_strikes_data=all_strikes_data, grid=(rows, snap_tol),
                n_side=int(n_side), native_gap=native_gap)
