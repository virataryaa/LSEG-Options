"""
app.py — Soft Options Dashboard (ICE Connect data) — OI Change + Volume
========================================================================
Commodities : KC (Coffee C) | CC (Cocoa) | SB (Sugar #11) | CT | LRC | LCC
Sidebar     : Old Date + New Date (shared)
Each Tab    : Min OI + ATM info, then OI Change (left) | Volume (right)
              butterfly tables, OI Snapshot, Drill-Down time series,
              and OI & Volume time series across all strikes.

Split out of the original monolithic app.py for load/response speed —
Px Change, Vol Surface, and IV vs RV now live in oi_advanced_analytics.py
(same folder). Shared code lives in common.py.
"""

import streamlit as st
import pandas as pd
import numpy as np

import common as c

st.set_page_config(page_title="Options Dashboard", layout="wide")

dfs, atm_data = c.load_core_data()
old_date, new_date = c.render_sidebar(dfs, title="Options Dashboard")


# ── Commodity tab renderer — OI Change + Volume only ───────────────────────────
def render_commodity_tab(df, atm_val, atm_label, old_date, new_date,
                         key_prefix, title, ric_fn, display_step=None, mround_default=None,
                         ingest_note=""):
    if df.empty:
        st.info(f"No data available for {title}.")
        return

    min_oi, custom_atm, custom_step, strike_mode, month_keys, all_strikes_data = c.render_controls(
        df, atm_val, atm_label, atm_data, key_prefix, title,
        display_step=display_step, mround_default=mround_default, ingest_note=ingest_note,
    )
    all_strikes, snap_tol = c.build_strike_grid(custom_atm, custom_step, strike_mode, all_strikes_data)

    call_oi  = c.get_oi_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
    put_oi   = c.get_oi_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)
    call_vol = c.get_vol_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
    put_vol  = c.get_vol_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)

    c_oi  = c._tot(call_oi);  p_oi  = c._tot(put_oi)
    c_vol = c._tot(call_vol); p_vol = c._tot(put_vol)
    # np.isnan guards explicitly — plain `!= 0` is True for NaN in Python,
    # which would have computed NaN/NaN and displayed the literal "nan".
    cp_oi  = (f"{abs(c_oi/p_oi):.2f}" if p_oi and not np.isnan(p_oi) and p_oi != 0 and not np.isnan(c_oi) else "—")
    cp_vol = (f"{c_vol/p_vol:.2f}"    if p_vol and not np.isnan(p_vol) and p_vol > 0 and not np.isnan(c_vol) else "—")

    items = [
        ("ATM Price",     f"{custom_atm:,.2f}"),
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
            c.butterfly_html(call_oi, put_oi, custom_atm, c.oi_color, month_keys,
                           fmt="{:.0f}", footer=True, title=title,
                           fixed_strikes=all_strikes, snap_tol=snap_tol),
            unsafe_allow_html=True)
    with cr:
        st.markdown("**Volume**")
        st.markdown(
            c.butterfly_html(call_vol, put_vol, custom_atm, c.vol_color, month_keys,
                           fmt="{:.0f}", footer=True, title=title,
                           fixed_strikes=all_strikes, snap_tol=snap_tol),
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
                c.butterfly_html(call_oi_old, put_oi_old, custom_atm, c.vol_color, month_keys,
                               fmt="{:.0f}", footer=False, title=title,
                               fixed_strikes=all_strikes, snap_tol=snap_tol),
                unsafe_allow_html=True)
        with sc2:
            st.markdown(f"**New Date — {new_date.strftime('%d %b %Y')}**")
            st.markdown(
                c.butterfly_html(call_oi_new, put_oi_new, custom_atm, c.vol_color, month_keys,
                               fmt="{:.0f}", footer=False, title=title,
                               fixed_strikes=all_strikes, snap_tol=snap_tol),
                unsafe_allow_html=True)

    with st.expander("Drill Down — Single Option Time Series"):
        call_dd_piv = c.get_oi_snapshot_pivot(df, month_keys, "Call", new_date, new_date, min_oi)
        put_dd_piv  = c.get_oi_snapshot_pivot(df, month_keys, "Put",  new_date, new_date, min_oi)

        col_labels = {mk: f"{c.MONTH_NAMES[mk[0]]} '{str(mk[1])[-2:]}" for mk in month_keys}
        mk_lookup  = {v: k for k, v in col_labels.items()}

        def _flat_list(piv):
            rows = []
            for strike in sorted(piv.index):
                for mk in month_keys:
                    if mk not in piv.columns:
                        continue
                    try:
                        v = float(piv.at[strike, mk])
                    except (TypeError, ValueError):
                        continue
                    if np.isnan(v) or v <= 0:
                        continue
                    rows.append({"Strike": strike, "Expiry": col_labels[mk], "OI": int(v)})
            return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["Strike", "Expiry", "OI"])

        def _style_oi(s, rgb):
            mx = s.max() if len(s) > 0 else 1.0
            if pd.isna(mx) or mx == 0: mx = 1.0
            return [f"background-color:rgba({rgb},{round(0.15+min(v/mx,1.0)*0.5,2)});color:#1a1a2e"
                    if pd.notna(v) and v > 0 else "" for v in s]

        call_flat = _flat_list(call_dd_piv)
        put_flat  = _flat_list(put_dd_piv)

        all_expiries = [col_labels[mk] for mk in month_keys]
        fc1, fc2 = st.columns([1, 3])
        with fc1:
            exp_filter = st.selectbox("Filter by Expiry", ["All"] + all_expiries,
                                      key=f"{key_prefix}_dd_exp_filter")

        if exp_filter != "All":
            call_show = call_flat[call_flat["Expiry"] == exp_filter].reset_index(drop=True)
            put_show  = put_flat[put_flat["Expiry"]  == exp_filter].reset_index(drop=True)
        else:
            call_show, put_show = call_flat, put_flat

        st.caption(f"OI as of **{new_date.strftime('%d %b %Y')}** — click a row to view its time series")
        ddc1, ddc2 = st.columns(2)

        def _fmt_strike(x):
            return f"{x:.1f}" if x % 1 != 0 else f"{int(x)}"

        has_iv = "impvol" in df.columns and df["impvol"].notna().any()

        with ddc1:
            st.markdown("**Calls**")
            call_evt = st.dataframe(
                call_show.style.apply(_style_oi, rgb="66,133,244", subset=["OI"])
                         .format({"Strike": _fmt_strike, "OI": "{:,}"}),
                on_select="rerun", selection_mode="single-row",
                key=f"{key_prefix}_dd_call", use_container_width=True, hide_index=True,
            )
        with ddc2:
            st.markdown("**Puts**")
            put_evt = st.dataframe(
                put_show.style.apply(_style_oi, rgb="220,75,75", subset=["OI"])
                        .format({"Strike": _fmt_strike, "OI": "{:,}"}),
                on_select="rerun", selection_mode="single-row",
                key=f"{key_prefix}_dd_put", use_container_width=True, hide_index=True,
            )

        sel_type = sel_strike = sel_mk = None
        c_rows = call_evt.selection.get("rows", [])
        p_rows = put_evt.selection.get("rows", [])

        if c_rows and not call_show.empty:
            row = call_show.iloc[c_rows[0]]
            sel_type, sel_strike, sel_mk = "Call", row["Strike"], mk_lookup.get(row["Expiry"])
        elif p_rows and not put_show.empty:
            row = put_show.iloc[p_rows[0]]
            sel_type, sel_strike, sel_mk = "Put", row["Strike"], mk_lookup.get(row["Expiry"])

        if sel_type and sel_strike is not None and sel_mk:
            ric = ric_fn(sel_strike, sel_mk[0], sel_mk[1], sel_type)
            rdf = df[df["ric"] == ric].sort_values("date")
            strike_lbl = _fmt_strike(sel_strike)
            exp_lbl    = f"{c.MONTH_NAMES[sel_mk[0]]} '{str(sel_mk[1])[-2:]}"
            friendly   = f"{title} {exp_lbl} {strike_lbl} {sel_type} ({ric})"
            st.caption(f"**{friendly}** — {len(rdf)} trading days")
            if rdf.empty:
                st.info(f"No data for {ric}")
            else:
                show_iv = has_iv and "impvol" in rdf.columns and rdf["impvol"].notna().any()
                cc1, cc2, cc3, cc4 = st.columns(4 if show_iv else 3)
                fields = [
                    (cc1, "oi",     "Open Interest"),
                    (cc2, "volume", "Volume"),
                    (cc3, "settle", "Settle Price"),
                ]
                if show_iv:
                    fields.append((cc4, "impvol", "Impl. Vol %"))
                for col, field, label in fields:
                    s = pd.to_numeric(rdf.set_index("date")[field], errors="coerce").dropna()
                    if not s.empty:
                        col.markdown(f"**{label}**")
                        if field == "volume":
                            col.bar_chart(s)
                        else:
                            col.line_chart(s)
        else:
            st.caption("Click any row above to view its time series.")

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
