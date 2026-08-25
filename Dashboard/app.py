"""
app.py — Soft Options Dashboard (ICE Connect data) — OI Change + Volume
========================================================================
Commodities : KC (Coffee C) | CC (Cocoa) | SB (Sugar #11) | CT | LRC | LCC
Sidebar     : Old Date + New Date (shared)
Each Tab    : Controls + KPI row, then OI Change (left) | Volume (right)
              butterfly tables, OI Snapshot, multi-select Drill Down, and
              OI & Volume time series across all strikes.

Px Change, Vol Surface, and IV vs RV live in oi_advanced_analytics.py.
Shared code lives in common.py.
"""

import streamlit as st
import pandas as pd
import numpy as np

import common as c

st.set_page_config(page_title="Options Dashboard", layout="wide")

dfs, atm_data = c.load_core_data()
old_date, new_date = c.render_sidebar(dfs, title="Options Dashboard")

MAX_DRILL = 8  # distinct colors available for overlaid series


def _drilldown(df, key_prefix, title, new_date, min_oi, month_keys):
    """Multi-select option picker + overlaid OI / Volume / Settle / ImpVol charts."""
    import plotly.graph_objects as go  # lazy — keeps cold start fast

    # OI is published a session late, so list from the most recent date that
    # actually has it rather than showing an empty picker.
    snap = c.latest_oi_date(df, on_or_before=new_date) or new_date
    if snap != new_date:
        st.caption(f"OI not published for {new_date.strftime('%d %b %Y')} — "
                   f"listing positions as of **{snap.strftime('%d %b %Y')}**.")

    col_labels = {mk: f"{c.MONTH_NAMES[mk[0]]} '{str(mk[1])[-2:]}" for mk in month_keys}

    def flat(opt):
        d = df[(df["date"].dt.date == snap) & (df["option_type"] == opt)][
            ["ric", "strike", "expiry_month", "expiry_year", "oi"]].copy()
        if d.empty:
            return pd.DataFrame(columns=["Strike", "Expiry", "OI", "ric"])
        d["OI"] = pd.to_numeric(d["oi"], errors="coerce")
        d = d[d["OI"] > 0]
        if min_oi > 0:
            d = d[d["OI"] >= min_oi]
        d["mk"] = list(zip(d["expiry_month"].astype(int), d["expiry_year"].astype(int)))
        d["Expiry"] = d["mk"].map(col_labels)
        d = d.dropna(subset=["Expiry"])
        return (d.rename(columns={"strike": "Strike"})
                 [["Strike", "Expiry", "OI", "ric"]]
                 .sort_values(["Strike", "Expiry"]).reset_index(drop=True))

    call_flat, put_flat = flat("Call"), flat("Put")

    all_expiries = [col_labels[mk] for mk in month_keys]
    fc1, fc2 = st.columns([1, 3])
    with fc1:
        exp_filter = st.selectbox("Filter by Expiry", ["All"] + all_expiries,
                                  key=f"{key_prefix}_dd_exp_filter")
    if exp_filter != "All":
        call_show = call_flat[call_flat["Expiry"] == exp_filter].reset_index(drop=True)
        put_show  = put_flat[put_flat["Expiry"] == exp_filter].reset_index(drop=True)
    else:
        call_show, put_show = call_flat, put_flat

    def style_oi(s, rgb):
        mx = s.max() if len(s) > 0 else 1.0
        if pd.isna(mx) or mx == 0: mx = 1.0
        return [f"background-color:rgba({rgb},{round(0.15+min(v/mx,1.0)*0.5,2)});color:#1a1a2e"
                if pd.notna(v) and v > 0 else "" for v in s]

    st.caption(f"OI as of **{snap.strftime('%d %b %Y')}** — "
               f"tick up to **{MAX_DRILL}** rows across both tables to overlay them.")

    # Streamlit exposes no API to clear a dataframe's row selection, and its
    # widget state is not writable from here. Rotating a nonce through the
    # widget key rebuilds both tables as fresh widgets, which is the reliable
    # way to land on an empty selection.
    nonce_key = f"{key_prefix}_dd_nonce"
    nonce = st.session_state.get(nonce_key, 0)
    call_key, put_key = f"{key_prefix}_dd_call_{nonce}", f"{key_prefix}_dd_put_{nonce}"

    ddc1, ddc2 = st.columns(2)
    with ddc1:
        st.markdown("**Calls**")
        call_evt = st.dataframe(
            call_show.drop(columns=["ric"]).style
                     .apply(style_oi, rgb="66,133,244", subset=["OI"])
                     .format({"Strike": c.fmt_strike, "OI": "{:,.0f}"}),
            on_select="rerun", selection_mode="multi-row",
            key=call_key, width="stretch", hide_index=True,
        )
    with ddc2:
        st.markdown("**Puts**")
        put_evt = st.dataframe(
            put_show.drop(columns=["ric"]).style
                    .apply(style_oi, rgb="220,75,75", subset=["OI"])
                    .format({"Strike": c.fmt_strike, "OI": "{:,.0f}"}),
            on_select="rerun", selection_mode="multi-row",
            key=put_key, width="stretch", hide_index=True,
        )

    picks = []
    for evt, show, opt in [(call_evt, call_show, "Call"), (put_evt, put_show, "Put")]:
        for i in evt.selection.get("rows", []):
            if i < len(show):
                r = show.iloc[i]
                picks.append(dict(
                    ric=r["ric"], opt=opt, strike=r["Strike"], expiry=r["Expiry"],
                    label=f"{c.fmt_strike(r['Strike'])} {opt} {r['Expiry']}"))

    bc1, bc2 = st.columns([1, 4])
    with bc1:
        if st.button(f"Clear selection ({len(picks)})", key=f"{key_prefix}_dd_clear",
                     disabled=not picks, width="stretch",
                     help="Deselect every option in both tables."):
            st.session_state[nonce_key] = nonce + 1
            # drop the retired widgets' state so repeated clears don't accumulate
            for stale in [k for k in st.session_state
                          if k.startswith((f"{key_prefix}_dd_call_", f"{key_prefix}_dd_put_"))
                          and not k.endswith(f"_{nonce + 1}")]:
                try:
                    del st.session_state[stale]
                except KeyError:
                    pass
            st.rerun()

    if not picks:
        st.caption("Tick rows above to chart them. Multiple selections overlay on one chart.")
        return

    if len(picks) > MAX_DRILL:
        st.warning(f"{len(picks)} rows selected — charting the first {MAX_DRILL}.")
        picks = picks[:MAX_DRILL]

    series = {p["ric"]: df[df["ric"] == p["ric"]].sort_values("date") for p in picks}
    has_iv = any("impvol" in s.columns and s["impvol"].notna().any() for s in series.values())

    # Volume is drawn as bars, not lines: it is a per-session quantity that is
    # frequently zero or unreported, and a line interpolates straight through
    # those gaps — reading as steady trade on days nothing changed hands.
    fields = [("oi", "Open Interest", "line"),
              ("volume", "Volume", "bar"),
              ("settle", "Settle Price", "line")]
    if has_iv:
        fields.append(("impvol", "Implied Vol %", "line"))

    figs = []
    for field, label, kind in fields:
        fig = go.Figure()
        drew = False
        for i, p in enumerate(picks):
            sdf = series[p["ric"]]
            if field not in sdf.columns:
                continue
            s = pd.to_numeric(sdf.set_index("date")[field], errors="coerce").dropna()
            if kind == "bar":
                s = s[s != 0]  # a zero-height bar is just noise on the axis
            if s.empty:
                continue
            drew = True
            color = c.SERIES_COLORS[i % len(c.SERIES_COLORS)]
            hover = (f"<b>{p['label']}</b><br>%{{x|%d %b %Y}}<br>"
                     f"{label}: %{{y:,.2f}}<extra></extra>")
            if kind == "bar":
                fig.add_trace(go.Bar(
                    x=s.index, y=s.values, name=p["label"],
                    marker=dict(color=color, line=dict(width=0)),
                    hovertemplate=hover,
                ))
            else:
                fig.add_trace(go.Scatter(
                    x=s.index, y=s.values, mode="lines", name=p["label"],
                    line=dict(color=color, width=1.8),
                    hovertemplate=hover,
                ))
        fig.update_layout(
            title=dict(text=label, x=0, font=dict(size=13)),
            height=300, margin=dict(l=45, r=15, t=35, b=35),
            xaxis_title=None, yaxis_title=None,
            legend=dict(orientation="h", y=-0.18, font=dict(size=9)),
            plot_bgcolor="#fafafa", paper_bgcolor="#fafafa",
            hovermode="x unified",
            # group so overlapping series sit side by side instead of hiding
            # each other; bargap keeps single-name days from looking like slabs
            barmode="group", bargap=0.15, bargroupgap=0.05,
        )
        if kind == "bar":
            fig.update_yaxes(rangemode="tozero")
        figs.append((fig, drew, label))

    st.caption(" · ".join(f"**{p['label']}** ({p['ric']}, {len(series[p['ric']])}d)" for p in picks))
    for a, b in [(0, 1), (2, 3)]:
        cols = st.columns(2)
        for col, idx in zip(cols, (a, b)):
            if idx >= len(figs):
                continue
            fig, drew, label = figs[idx]
            with col:
                if drew:
                    st.plotly_chart(fig, width="stretch",
                                    key=f"{key_prefix}_dd_fig_{idx}")
                else:
                    st.info(f"No {label} data for the selected options.")


def render_commodity_tab(df, atm_val, atm_label, old_date, new_date,
                         key_prefix, title, ric_fn, display_step=None,
                         mround_default=None, ingest_note=""):
    if df.empty:
        st.info(f"No data available for {title}.")
        return

    cfg = c.render_controls(
        df, atm_val, atm_label, atm_data, key_prefix, title,
        display_step=display_step, mround_default=mround_default, ingest_note=ingest_note,
    )
    min_oi     = cfg["min_oi"]
    custom_atm = cfg["custom_atm"]
    month_keys = cfg["month_keys"]
    grid       = cfg["grid"]

    c.oi_notice(df, new_date, title)

    call_oi  = c.get_oi_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
    put_oi   = c.get_oi_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)
    call_vol = c.get_vol_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
    put_vol  = c.get_vol_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)

    # KPIs are computed from the projected (visible) grid so they agree with the
    # table footers instead of silently including strikes that are off-screen.
    rows, tol = grid
    vis = {k: c.project_to_grid(p, rows, tol, how="sum") for k, p in
           dict(coi=call_oi, poi=put_oi, cvol=call_vol, pvol=put_vol).items()}
    c_oi, p_oi   = c._tot(vis["coi"]),  c._tot(vis["poi"])
    c_vol, p_vol = c._tot(vis["cvol"]), c._tot(vis["pvol"])
    # np.isnan guards explicitly — plain `!= 0` is True for NaN in Python,
    # which would have computed NaN/NaN and displayed the literal "nan".
    cp_oi  = (f"{abs(c_oi/p_oi):.2f}" if p_oi and not np.isnan(p_oi) and p_oi != 0 and not np.isnan(c_oi) else "—")
    cp_vol = (f"{c_vol/p_vol:.2f}"    if p_vol and not np.isnan(p_vol) and p_vol > 0 and not np.isnan(c_vol) else "—")

    items = [
        ("ATM Price",     f"{custom_atm:,.4g}"),
        ("Call OI Delta", c._fn(c_oi)),
        ("Put OI Delta",  c._fn(p_oi)),
        ("Call Volume",   c._fn(c_vol)),
        ("Put Volume",    c._fn(p_vol)),
        ("C/P OI Ratio",  cp_oi),
        ("C/P Vol Ratio", cp_vol),
    ]
    st.markdown(
        '<div style="display:flex;gap:28px;padding:6px 0 12px;border-bottom:1px solid #eee;flex-wrap:wrap">'
        + "".join(
            f'<div><div style="font-size:9px;color:#888;letter-spacing:.07em;'
            f'text-transform:uppercase;margin-bottom:2px">{lbl}</div>'
            f'<div style="font-size:14px;font-weight:600;color:#1a1a2e">{val}</div></div>'
            for lbl, val in items
        )
        + '</div>',
        unsafe_allow_html=True
    )

    cl, cr = st.columns(2)
    with cl:
        st.markdown("**OI Change**")
        st.markdown(
            c.render_butterfly(call_oi, put_oi, grid, custom_atm, c.oi_color, month_keys,
                               how="sum", fmt="{:.0f}", footer=True, title=title),
            unsafe_allow_html=True)
    with cr:
        st.markdown("**Volume**")
        st.markdown(
            c.render_butterfly(call_vol, put_vol, grid, custom_atm, c.vol_color, month_keys,
                               how="sum", fmt="{:.0f}", footer=True, title=title),
            unsafe_allow_html=True)

    with st.expander("OI Snapshot — Old Date vs New Date"):
        call_oi_old = c.get_oi_snapshot_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
        put_oi_old  = c.get_oi_snapshot_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)
        call_oi_new = c.get_oi_snapshot_pivot(df, month_keys, "Call", new_date, new_date, min_oi)
        put_oi_new  = c.get_oi_snapshot_pivot(df, month_keys, "Put",  new_date, new_date, min_oi)
        sc1, sc2 = st.columns(2)
        with sc1:
            st.markdown(f"**Old Date — {old_date.strftime('%d %b %Y')}**")
            st.markdown(
                c.render_butterfly(call_oi_old, put_oi_old, grid, custom_atm, c.vol_color,
                                   month_keys, how="sum", fmt="{:.0f}", footer=True, title=title),
                unsafe_allow_html=True)
        with sc2:
            st.markdown(f"**New Date — {new_date.strftime('%d %b %Y')}**")
            st.markdown(
                c.render_butterfly(call_oi_new, put_oi_new, grid, custom_atm, c.vol_color,
                                   month_keys, how="sum", fmt="{:.0f}", footer=True, title=title),
                unsafe_allow_html=True)

    with st.expander("Drill Down — Option Time Series (multi-select)"):
        _drilldown(df, key_prefix, title, new_date, min_oi, month_keys)

    with st.expander("OI & Volume Time Series — All Strikes"):
        all_d = sorted(df["date"].dt.date.unique())
        if len(all_d) >= 2:
            dr = st.slider("Date Range", min_value=all_d[0], max_value=all_d[-1],
                           value=(all_d[0], all_d[-1]), key=f"{key_prefix}_ts_dr")
            sub = df[(df["date"].dt.date >= dr[0]) & (df["date"].dt.date <= dr[1])].copy()
            # min_count=1: a date where OI is null across every strike (LSEG
            # publishes OI a day behind Settle/Volume, so the latest date is
            # routinely all-null) must sum to NaN, not 0 — plain .sum()
            # treats an all-NaN group as 0, which drew a false plunge to zero
            # on the most recent point instead of leaving it as a gap.
            daily = (sub.groupby(["date", "option_type"])
                     .agg(oi=("oi", lambda s: s.sum(min_count=1)),
                          volume=("volume", lambda s: s.sum(min_count=1)))
                     .reset_index())
            tc1, tc2 = st.columns(2)
            with tc1:
                st.markdown("**Call / Put OI**")
                oi_w = daily.pivot(index="date", columns="option_type", values="oi")
                oi_w.columns.name = None
                st.line_chart(oi_w.rename(columns={"Call": "Call OI", "Put": "Put OI"}))
            with tc2:
                st.markdown("**Call / Put Volume**")
                vol_w = daily.pivot(index="date", columns="option_type", values="volume")
                vol_w.columns.name = None
                st.line_chart(vol_w.rename(columns={"Call": "Call Vol", "Put": "Put Vol"}))


# ── Main layout ────────────────────────────────────────────────────────────────
st.title("Options Dashboard")
st.caption(
    f"Old Date: **{old_date.strftime('%d %b %Y')}**  |  "
    f"New Date: **{new_date.strftime('%d %b %Y')}**  |  "
    f"Advanced analytics (Px Change, Vol Surface, IV vs RV) → run `oi_advanced_analytics.py`"
)

tabs = st.tabs([cm["tab_label"] for cm in c.COMMODITIES])

for tab, cm in zip(tabs, c.COMMODITIES):
    with tab:
        atm_val = atm_data.get(cm["key"])
        atm_label = cm["atm_fmt"](atm_val) if atm_val is not None else "—"
        render_commodity_tab(
            df=dfs[cm["key"]], atm_val=atm_val, atm_label=atm_label,
            old_date=old_date, new_date=new_date,
            key_prefix=cm["key"].lower(), title=cm["title"], ric_fn=cm["ric_fn"],
            display_step=cm["display_step"], mround_default=cm["mround_default"],
            ingest_note=cm["ingest_note"],
        )
