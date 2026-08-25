"""
oi_advanced_analytics.py — Soft Options Dashboard: Advanced Analytics
========================================================================
Commodities : KC (Coffee C) | CC (Cocoa) | SB (Sugar #11) | CT | LRC | LCC
Sidebar     : Old Date + New Date (shared)
Each Tab    : Px Change / % Change, Vol Surface (ImpVol snapshot + change,
              Vol Smile, Term Structure), and IV vs RV.

Split out of the original monolithic app.py — OI Change + Volume now lives
in app.py (same folder), which stays fast since it no longer loads futures
data or computes ImpVol/RV panels. Shared code lives in common.py.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go

import common as c

st.set_page_config(page_title="Options Dashboard — Advanced Analytics", layout="wide")

dfs, atm_data = c.load_core_data()
old_date, new_date = c.render_sidebar(dfs, title="OI Advanced Analytics")


# ── Commodity tab renderer — Px Change / Vol Surface / IV vs RV ────────────────
def render_commodity_tab(df, atm_val, atm_label, old_date, new_date,
                         key_prefix, title, display_step=None, mround_default=None,
                         ingest_note="", fut_df=None):
    if df.empty:
        st.info(f"No data available for {title}.")
        return

    min_oi, custom_atm, custom_step, strike_mode, month_keys, all_strikes_data = c.render_controls(
        df, atm_val, atm_label, atm_data, key_prefix, title,
        display_step=display_step, mround_default=mround_default, ingest_note=ingest_note,
    )
    all_strikes, snap_tol = c.build_strike_grid(custom_atm, custom_step, strike_mode, all_strikes_data)

    has_iv = "impvol" in df.columns and df["impvol"].notna().any()
    has_fut = fut_df is not None and not fut_df.empty
    tab_labels = ["Px Change"]
    if has_iv:
        tab_labels.append("Vol Surface (Proof of Concept)")
    if has_iv and has_fut:
        tab_labels.append("IV vs RV")
    inner_tabs = st.tabs(tab_labels)
    inner_px = inner_tabs[0]
    inner_vs = inner_tabs[1] if has_iv else None
    inner_rv = inner_tabs[2] if (has_iv and has_fut) else None

    with inner_px:
        call_px  = c.get_px_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
        put_px   = c.get_px_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)
        call_pct = c.get_pct_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
        put_pct  = c.get_pct_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)

        pc1, pc2 = st.columns(2)
        with pc1:
            st.markdown("**Px Change**")
            st.markdown(
                c.butterfly_html(call_px, put_px, custom_atm, c.px_color, month_keys,
                               fmt="{:.2f}", footer=False, title=title,
                               fixed_strikes=all_strikes, snap_tol=snap_tol),
                unsafe_allow_html=True)
        with pc2:
            st.markdown("**% Change**")
            st.markdown(
                c.butterfly_html(call_pct, put_pct, custom_atm, c.px_color, month_keys,
                               fmt="{:.1f}", footer=False, sfx="%", title=title,
                               fixed_strikes=all_strikes, snap_tol=snap_tol),
                unsafe_allow_html=True)

    if inner_vs is not None:
        with inner_vs:
            # ── Row 1: ImpVol snapshot + IV Change butterflies ────────────────
            vc1, vc2 = st.columns(2)
            call_iv     = c.get_iv_pivot(df, month_keys, "Call", new_date, min_oi)
            put_iv      = c.get_iv_pivot(df, month_keys, "Put",  new_date, min_oi)
            call_iv_chg = c.get_iv_change_pivot(df, month_keys, "Call", old_date, new_date, min_oi)
            put_iv_chg  = c.get_iv_change_pivot(df, month_keys, "Put",  old_date, new_date, min_oi)

            with vc1:
                st.markdown(f"**ImpVol Snapshot — {new_date.strftime('%d %b %Y')}**")
                st.markdown(
                    c.butterfly_html(call_iv, put_iv, custom_atm, c.iv_color, month_keys,
                                   fmt="{:.1f}", sfx="%", footer=False, title=title,
                                   fixed_strikes=all_strikes, snap_tol=snap_tol),
                    unsafe_allow_html=True)
            with vc2:
                st.markdown(f"**IV Change — {old_date.strftime('%d %b %Y')} → {new_date.strftime('%d %b %Y')}**")
                st.markdown(
                    c.butterfly_html(call_iv_chg, put_iv_chg, custom_atm, c.iv_chg_color, month_keys,
                                   fmt="{:+.1f}", sfx="%", footer=False, title=title,
                                   fixed_strikes=all_strikes, snap_tol=snap_tol),
                    unsafe_allow_html=True)

            st.divider()

            # ── Row 2: Vol Smile chart ────────────────────────────────────────
            with st.expander("Vol Smile — ImpVol by Strike", expanded=True):
                col_labels = {mk: f"{c.MONTH_NAMES[mk[0]]} '{str(mk[1])[-2:]}" for mk in month_keys}
                smile_exp  = st.selectbox(
                    "Expiry", [col_labels[mk] for mk in month_keys],
                    key=f"{key_prefix}_smile_exp"
                )
                mk_lookup_smile = {v: k for k, v in col_labels.items()}
                sel_mk = mk_lookup_smile.get(smile_exp)

                if sel_mk:
                    sub_iv = df[
                        (df["date"].dt.date == new_date) &
                        (df["expiry_month"] == sel_mk[0]) &
                        (df["expiry_year"]  == sel_mk[1]) &
                        df["impvol"].notna()
                    ].copy()

                    if not sub_iv.empty:
                        calls_smile = sub_iv[sub_iv["option_type"] == "Call"].sort_values("strike")
                        puts_smile  = sub_iv[sub_iv["option_type"] == "Put"].sort_values("strike")

                        fig = go.Figure()
                        fig.add_trace(go.Scatter(
                            x=calls_smile["strike"], y=calls_smile["impvol"],
                            mode="lines+markers", name="Call IV",
                            line=dict(color="#4285f4", width=2),
                            marker=dict(size=5)
                        ))
                        fig.add_trace(go.Scatter(
                            x=puts_smile["strike"], y=puts_smile["impvol"],
                            mode="lines+markers", name="Put IV",
                            line=dict(color="#dc4b4b", width=2),
                            marker=dict(size=5)
                        ))
                        if custom_atm:
                            fig.add_vline(x=custom_atm, line_dash="dash",
                                          line_color="#f59e0b", line_width=1.5,
                                          annotation_text="ATM", annotation_position="top right")
                        all_iv_vals = pd.concat([calls_smile["impvol"], puts_smile["impvol"]]).dropna()
                        iv_lo = max(0, all_iv_vals.min() - 3)
                        iv_hi = all_iv_vals.max() + 3
                        fig.update_layout(
                            height=340, margin=dict(l=40, r=20, t=30, b=40),
                            xaxis_title="Strike", yaxis_title="Implied Vol %",
                            yaxis=dict(range=[iv_lo, iv_hi]),
                            legend=dict(orientation="h", y=1.1),
                            plot_bgcolor="#fafafa", paper_bgcolor="#fafafa"
                        )
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No ImpVol data for selected expiry on this date.")

            # ── Row 2b: Term Structure Snapshot ──────────────────────────────
            with st.expander("Vol Term Structure — ATM IV across expiries (snapshot)", expanded=True):
                def _ts_snapshot(snap_date, fut_df, custom_atm):
                    """ATM IV per expiry on one date. Returns sorted DataFrame."""
                    if "impvol" not in df.columns:
                        return pd.DataFrame()
                    sub = df[(df["date"].dt.date == snap_date) & df["impvol"].notna()].copy()
                    if sub.empty:
                        return pd.DataFrame()
                    sub["mk_label"] = (sub["expiry_month"].map(c.MONTH_NAMES)
                                       + " '" + sub["expiry_year"].astype(str).str[-2:])
                    sub["sort_key"] = sub["expiry_year"] * 100 + sub["expiry_month"]

                    if fut_df is not None and not fut_df.empty:
                        fut_month_ints = sorted(fut_df["month_int"].dropna().unique().tolist())
                        unique_exp = sub[["expiry_month","expiry_year"]].drop_duplicates()
                        exp_to_fut = {}
                        for _, r in unique_exp.iterrows():
                            em, ey = int(r.expiry_month), int(r.expiry_year)
                            fm = next((m for m in fut_month_ints if m >= em), fut_month_ints[0])
                            fy = ey if any(m >= em for m in fut_month_ints) else ey + 1
                            exp_to_fut[(em, ey)] = (fm, fy)
                        sub["_fut_m"] = sub.apply(lambda r: exp_to_fut.get(
                            (int(r.expiry_month), int(r.expiry_year)), (None, None))[0], axis=1)
                        sub["_fut_y"] = sub.apply(lambda r: exp_to_fut.get(
                            (int(r.expiry_month), int(r.expiry_year)), (None, None))[1], axis=1)
                        fut_day = (fut_df[fut_df["Date"].dt.date == snap_date]
                                   .rename(columns={"month_int": "_fut_m", "year": "_fut_y"}))
                        sub = sub.merge(fut_day[["_fut_m", "_fut_y", "settlement"]],
                                        on=["_fut_m", "_fut_y"], how="left")
                        sub["settlement"] = sub["settlement"].fillna(custom_atm)
                        sub["atm_dist"] = (sub["strike"] - sub["settlement"]).abs()
                    else:
                        sub["settlement"] = custom_atm
                        sub["atm_dist"] = (sub["strike"] - custom_atm).abs()

                    has_futures = fut_df is not None and not fut_df.empty
                    rows = []
                    # Call and Put each pick their OWN nearest-to-anchor strike with a
                    # live impvol reading, instead of being forced onto one shared
                    # "ATM strike" — a thin expiry often has calls quoted near ATM but
                    # no put trading at that exact strike (or vice versa), and forcing
                    # both onto the same strike created a gap even when a put existed
                    # just one strike away. This doesn't fix a genuinely one-sided
                    # expiry (no puts at all that day) — nothing can — but it recovers
                    # the cases where the other side just wasn't at the identical strike.
                    for (lbl, sk), grp in sub.groupby(["mk_label", "sort_key"]):
                        anchor = float(grp["settlement"].iloc[0])  # same for the whole expiry group
                        calls = grp[(grp["option_type"] == "Call") & grp["impvol"].notna()]
                        puts  = grp[(grp["option_type"] == "Put")  & grp["impvol"].notna()]
                        iv_c = iv_p = np.nan
                        strike_c = strike_p = None
                        if not calls.empty:
                            i = calls["atm_dist"].idxmin()
                            iv_c, strike_c = calls.at[i, "impvol"], calls.at[i, "strike"]
                        if not puts.empty:
                            i = puts["atm_dist"].idxmin()
                            iv_p, strike_p = puts.at[i, "impvol"], puts.at[i, "strike"]
                        rows.append({"mk_label": lbl, "sort_key": sk,
                                     "iv_call": iv_c, "iv_put": iv_p,
                                     "iv_avg": float(pd.Series([iv_c, iv_p]).mean()),
                                     "anchor_px": anchor,
                                     "call_strike": strike_c, "put_strike": strike_p,
                                     "anchor_src": "Futures settlement" if has_futures else "ATM snap"})
                    return pd.DataFrame(rows).sort_values("sort_key").reset_index(drop=True)

                snap_new = _ts_snapshot(new_date, fut_df, custom_atm)
                snap_old = _ts_snapshot(old_date, fut_df, custom_atm)

                # OI per expiry on new_date. min_count=1 so an expiry with OI
                # null across every strike on this date (LSEG publishes OI a
                # day behind Settle/Volume, so the latest date is routinely
                # all-null) sums to NaN, not a misleading 0 bar.
                oi_snap = (
                    df[df["date"].dt.date == new_date]
                    .assign(mk_label=lambda d: d["expiry_month"].map(c.MONTH_NAMES)
                                               + " '" + d["expiry_year"].astype(str).str[-2:],
                            sort_key=lambda d: d["expiry_year"] * 100 + d["expiry_month"])
                    .groupby(["mk_label", "sort_key"], as_index=False)["oi"]
                    .sum(min_count=1)
                    .sort_values("sort_key")
                )

                if snap_new.empty:
                    st.info("No ImpVol data available for term structure snapshot.")
                else:
                    all_snap_vals = pd.concat([
                        snap_new[["iv_call","iv_put"]].stack(),
                        snap_old[["iv_call","iv_put"]].stack() if not snap_old.empty else pd.Series(dtype=float)
                    ]).dropna()
                    sn_lo = max(0, float(all_snap_vals.min()) - 2)
                    sn_hi = float(all_snap_vals.max()) + 2

                    fig_sn = go.Figure()

                    # OI bars on secondary axis — plotted first so IV lines sit on top
                    if not oi_snap.empty:
                        fig_sn.add_trace(go.Bar(
                            x=oi_snap["mk_label"], y=oi_snap["oi"],
                            name="Total OI", yaxis="y2",
                            marker_color="rgba(156,163,175,0.35)",
                            marker_line=dict(color="rgba(156,163,175,0.6)", width=1),
                            showlegend=True
                        ))

                    call_hover = (
                        "<b>%{x}</b><br>Call IV: %{y:.1f}%<br>"
                        "Anchor: %{customdata[0]:,.2f} (call strike %{customdata[1]}, %{customdata[2]})<extra></extra>"
                    )
                    put_hover = (
                        "<b>%{x}</b><br>Put IV: %{y:.1f}%<br>"
                        "Anchor: %{customdata[0]:,.2f} (put strike %{customdata[1]}, %{customdata[2]})<extra></extra>"
                    )
                    fig_sn.add_trace(go.Scatter(
                        x=snap_new["mk_label"], y=snap_new["iv_call"],
                        mode="lines+markers", name=f"Call IV ({new_date})",
                        line=dict(color="#4285f4", width=2), marker=dict(size=7),
                        yaxis="y1",
                        customdata=snap_new[["anchor_px", "call_strike", "anchor_src"]].values,
                        hovertemplate=call_hover,
                    ))
                    fig_sn.add_trace(go.Scatter(
                        x=snap_new["mk_label"], y=snap_new["iv_put"],
                        mode="lines+markers", name=f"Put IV ({new_date})",
                        line=dict(color="#dc4b4b", width=2), marker=dict(size=7),
                        yaxis="y1",
                        customdata=snap_new[["anchor_px", "put_strike", "anchor_src"]].values,
                        hovertemplate=put_hover,
                    ))
                    if not snap_old.empty:
                        fig_sn.add_trace(go.Scatter(
                            x=snap_old["mk_label"], y=snap_old["iv_avg"],
                            mode="lines+markers", name=f"Avg IV ({old_date})",
                            line=dict(color="#9ca3af", width=1.5, dash="dash"),
                            marker=dict(size=5), yaxis="y1"
                        ))

                    fig_sn.update_layout(
                        height=380, margin=dict(l=40, r=60, t=30, b=60),
                        xaxis_title="Expiry",
                        yaxis=dict(title="Implied Vol %", range=[sn_lo, sn_hi], side="left"),
                        yaxis2=dict(title="Total OI (lots)", overlaying="y", side="right",
                                    showgrid=False, rangemode="tozero"),
                        legend=dict(orientation="h", y=-0.28),
                        plot_bgcolor="#fafafa", paper_bgcolor="#fafafa",
                        barmode="overlay"
                    )
                    st.plotly_chart(fig_sn, use_container_width=True)
                    st.caption(
                        "Call IV and Put IV each use the nearest strike to the anchor "
                        "price that actually has a live reading — the two can differ "
                        "if one side isn't trading at the exact ATM strike (left axis). "
                        "Grey bars = total OI across all strikes (right axis)."
                    )
                    def _fmt_strike(r, side):
                        v = r.call_strike if side == "C" else r.put_strike
                        return f"{v:g}" if v is not None and not pd.isna(v) else "—"
                    anchor_line = "  |  ".join(
                        f"{r.mk_label}: {r.anchor_px:,.2f} "
                        f"(call {_fmt_strike(r,'C')} / put {_fmt_strike(r,'P')}, {r.anchor_src})"
                        for r in snap_new.itertuples()
                    )
                    st.caption(f"**Live price used per expiry ({new_date}):** {anchor_line}")

            # ── Row 3: ATM Vol Term Structure ─────────────────────────────────
            with st.expander("ATM Vol Term Structure — ImpVol at ATM across expiries"):
                all_d_iv = sorted(df[df["impvol"].notna()]["date"].dt.date.unique())
                if len(all_d_iv) >= 2:
                    dr_iv = st.slider("Date Range", min_value=all_d_iv[0], max_value=all_d_iv[-1],
                                      value=(all_d_iv[0], all_d_iv[-1]), key=f"{key_prefix}_iv_dr")
                    sub_ts = df[
                        (df["date"].dt.date >= dr_iv[0]) &
                        (df["date"].dt.date <= dr_iv[1]) &
                        df["impvol"].notna()
                    ].copy()

                    # For each date × expiry, find the strike nearest to ATM and take its IV
                    sub_ts["mk_label"] = (sub_ts["expiry_month"].map(c.MONTH_NAMES)
                                          + " '" + sub_ts["expiry_year"].astype(str).str[-2:])

                    # Per-expiry ATM: use each expiry's own futures settlement price.
                    # Serial months (e.g. KC M/Q) map to the next available futures month.
                    # Falls back to custom_atm if futures parquet not available.
                    if fut_df is not None and not fut_df.empty:
                        fut_month_ints = sorted(fut_df["month_int"].dropna().unique().tolist())
                        unique_exp = sub_ts[["expiry_month","expiry_year"]].drop_duplicates()
                        exp_to_fut = {}
                        for _, r in unique_exp.iterrows():
                            em, ey = int(r.expiry_month), int(r.expiry_year)
                            fm = next((m for m in fut_month_ints if m >= em), fut_month_ints[0])
                            fy = ey if any(m >= em for m in fut_month_ints) else ey + 1
                            exp_to_fut[(em, ey)] = (fm, fy)
                        sub_ts["_fut_m"] = sub_ts.apply(
                            lambda r: exp_to_fut.get((int(r.expiry_month), int(r.expiry_year)), (None, None))[0], axis=1)
                        sub_ts["_fut_y"] = sub_ts.apply(
                            lambda r: exp_to_fut.get((int(r.expiry_month), int(r.expiry_year)), (None, None))[1], axis=1)
                        fut_settle = (fut_df.rename(columns={"Date": "date"})
                                      .rename(columns={"month_int": "_fut_m", "year": "_fut_y"}))
                        sub_ts = sub_ts.merge(
                            fut_settle[["date", "_fut_m", "_fut_y", "settlement"]],
                            on=["date", "_fut_m", "_fut_y"], how="left"
                        )
                        sub_ts["settlement"] = sub_ts["settlement"].fillna(custom_atm)
                        sub_ts["atm_dist"] = (sub_ts["strike"] - sub_ts["settlement"]).abs()
                    else:
                        sub_ts["atm_dist"] = (sub_ts["strike"] - custom_atm).abs()

                    atm_iv_ts = (sub_ts.sort_values("atm_dist")
                                       .groupby(["date", "mk_label"])
                                       .first()
                                       .reset_index()[["date", "mk_label", "impvol"]])
                    atm_iv_ts["date"] = pd.to_datetime(atm_iv_ts["date"])

                    pivot_ts = atm_iv_ts.pivot(index="date", columns="mk_label", values="impvol")
                    pivot_ts.columns.name = None
                    if not pivot_ts.empty:
                        ts_vals = pivot_ts.values.flatten()
                        ts_vals = ts_vals[~pd.isna(ts_vals)]
                        ts_lo   = max(0, float(ts_vals.min()) - 3) if len(ts_vals) else 0
                        ts_hi   = float(ts_vals.max()) + 3         if len(ts_vals) else 50

                        fig_ts = go.Figure()
                        colors = ["#4285f4","#dc4b4b","#f59e0b","#34a853","#8b5cf6","#06b6d4","#f97316"]
                        for i, col in enumerate(pivot_ts.columns):
                            s = pivot_ts[col].dropna()
                            fig_ts.add_trace(go.Scatter(
                                x=s.index, y=s.values,
                                mode="lines", name=col,
                                line=dict(color=colors[i % len(colors)], width=1.8)
                            ))
                        fig_ts.update_layout(
                            height=340, margin=dict(l=40, r=20, t=30, b=40),
                            xaxis_title="Date", yaxis_title="Implied Vol %",
                            yaxis=dict(range=[ts_lo, ts_hi]),
                            legend=dict(orientation="h", y=-0.2),
                            plot_bgcolor="#fafafa", paper_bgcolor="#fafafa"
                        )
                        st.plotly_chart(fig_ts, use_container_width=True)
                        src = "per-expiry futures settlement" if fut_df is not None and not fut_df.empty else "ATM snap (futures unavailable)"
                        st.caption(f"ATM anchored to {src} for each expiry — serial months mapped to next available futures contract.")
                else:
                    st.info("Not enough ImpVol history to plot term structure.")

    if inner_rv is not None:
        with inner_rv:
            rv_window_label = st.radio(
                "Realized vol window", ["10d", "20d", "30d", "60d"], index=1, horizontal=True,
                key=f"{key_prefix}_rv_window",
                help="Trailing window of daily log returns used to compute realized vol, annualized (×√252)."
            )
            rv_window = int(rv_window_label.replace("d", ""))

            all_d_rv = sorted(df[df["impvol"].notna()]["date"].dt.date.unique())
            if len(all_d_rv) < 2:
                st.info("Not enough ImpVol history to compare against realized vol.")
            else:
                dr_rv = st.slider("Date Range", min_value=all_d_rv[0], max_value=all_d_rv[-1],
                                   value=(all_d_rv[0], all_d_rv[-1]), key=f"{key_prefix}_rv_dr")

                col_labels_rv = {mk: f"{c.MONTH_NAMES[mk[0]]} '{str(mk[1])[-2:]}" for mk in month_keys}
                mk_lookup_rv  = {v: k for k, v in col_labels_rv.items()}
                sel_exp_rv = st.multiselect(
                    "Expiries to show", list(col_labels_rv.values()),
                    default=list(col_labels_rv.values())[:3], key=f"{key_prefix}_rv_exp"
                )

                sub_rv = df[
                    (df["date"].dt.date >= dr_rv[0]) & (df["date"].dt.date <= dr_rv[1]) &
                    df["impvol"].notna()
                ].copy()
                sub_rv["mk_label"] = (sub_rv["expiry_month"].map(c.MONTH_NAMES)
                                      + " '" + sub_rv["expiry_year"].astype(str).str[-2:])

                # Same Regular/Serial -> next-listed-futures-month mapping used for
                # ATM anchoring elsewhere in this tab (see Row 2b/3 above) — a serial
                # expiry's IV is compared against the realized vol of the SAME
                # underlying futures contract it settles against, not its own price
                # history (options don't have one; they cash/physically settle into
                # the futures contract).
                fut_month_ints = sorted(fut_df["month_int"].dropna().unique().tolist())
                unique_exp_rv = sub_rv[["expiry_month", "expiry_year"]].drop_duplicates()
                exp_to_fut_rv = {}
                for _, r in unique_exp_rv.iterrows():
                    em, ey = int(r.expiry_month), int(r.expiry_year)
                    fm = next((m for m in fut_month_ints if m >= em), fut_month_ints[0])
                    fy = ey if any(m >= em for m in fut_month_ints) else ey + 1
                    exp_to_fut_rv[(em, ey)] = (fm, fy)
                sub_rv["_fut_m"] = sub_rv.apply(
                    lambda r: exp_to_fut_rv.get((int(r.expiry_month), int(r.expiry_year)), (None, None))[0], axis=1)
                sub_rv["_fut_y"] = sub_rv.apply(
                    lambda r: exp_to_fut_rv.get((int(r.expiry_month), int(r.expiry_year)), (None, None))[1], axis=1)
                fut_settle_rv = (fut_df.rename(columns={"Date": "date"})
                                 .rename(columns={"month_int": "_fut_m", "year": "_fut_y"}))
                sub_rv = sub_rv.merge(fut_settle_rv[["date", "_fut_m", "_fut_y", "settlement"]],
                                      on=["date", "_fut_m", "_fut_y"], how="left")
                sub_rv["settlement"] = sub_rv["settlement"].fillna(custom_atm)
                sub_rv["atm_dist"] = (sub_rv["strike"] - sub_rv["settlement"]).abs()

                atm_iv_rv = (sub_rv.sort_values("atm_dist")
                             .groupby(["date", "mk_label"]).first()
                             .reset_index()[["date", "mk_label", "impvol"]])
                atm_iv_rv["date"] = pd.to_datetime(atm_iv_rv["date"])
                iv_pivot_rv = atm_iv_rv.pivot(index="date", columns="mk_label", values="impvol")
                iv_pivot_rv.columns.name = None

                # Realized vol per underlying futures contract: daily log returns,
                # rolling std over rv_window trading days, annualized.
                fut_hist = fut_df.rename(columns={"Date": "date"}).copy()
                fut_hist["date"] = pd.to_datetime(fut_hist["date"])
                rv_by_contract = {}
                for (fm, fy) in set(exp_to_fut_rv.values()):
                    fc = fut_hist[(fut_hist["month_int"] == fm) & (fut_hist["year"] == fy)].sort_values("date")
                    fc = fc[fc["settlement"].notna()]
                    if len(fc) < rv_window + 1:
                        continue
                    log_ret = np.log(fc["settlement"].astype(float) / fc["settlement"].astype(float).shift(1))
                    rv = log_ret.rolling(rv_window).std() * np.sqrt(252) * 100
                    rv_by_contract[(fm, fy)] = pd.Series(rv.values, index=fc["date"].values).dropna()

                if not sel_exp_rv:
                    st.info("Select at least one expiry above.")
                else:
                    fig_rv = go.Figure()
                    colors = ["#4285f4", "#dc4b4b", "#f59e0b", "#34a853", "#8b5cf6", "#06b6d4", "#f97316"]
                    any_trace = False
                    for i, mk_label in enumerate(sel_exp_rv):
                        color = colors[i % len(colors)]
                        if mk_label in iv_pivot_rv.columns:
                            s = iv_pivot_rv[mk_label].dropna()
                            if not s.empty:
                                any_trace = True
                                fig_rv.add_trace(go.Scatter(
                                    x=s.index, y=s.values, mode="lines", name=f"{mk_label} IV",
                                    line=dict(color=color, width=1.8)
                                ))
                        match = mk_lookup_rv.get(mk_label)
                        fut_key = exp_to_fut_rv.get(match) if match else None
                        if fut_key and fut_key in rv_by_contract:
                            rv_s = rv_by_contract[fut_key]
                            rv_s = rv_s[(rv_s.index.normalize() >= pd.Timestamp(dr_rv[0])) &
                                        (rv_s.index.normalize() <= pd.Timestamp(dr_rv[1]))]
                            if not rv_s.empty:
                                any_trace = True
                                fig_rv.add_trace(go.Scatter(
                                    x=rv_s.index, y=rv_s.values, mode="lines",
                                    name=f"{mk_label} RV ({rv_window}d)",
                                    line=dict(color=color, width=1.8, dash="dot")
                                ))

                    if not any_trace:
                        st.info("No IV or realized-vol data in the selected range for these expiries.")
                    else:
                        fig_rv.update_layout(
                            height=380, margin=dict(l=40, r=20, t=30, b=40),
                            xaxis_title="Date", yaxis_title="Vol %",
                            legend=dict(orientation="h", y=-0.25),
                            plot_bgcolor="#fafafa", paper_bgcolor="#fafafa"
                        )
                        st.plotly_chart(fig_rv, use_container_width=True)
                        st.caption(
                            f"Solid = ATM implied vol per expiry. Dotted = {rv_window}-day realized vol "
                            "(annualized, from daily log returns) of the underlying futures contract each "
                            "expiry settles against — serial-month expiries share their Regular contract's "
                            "realized vol, same mapping as the ATM anchoring above. A large IV-over-RV gap "
                            "means options are pricing in more movement than has actually happened; IV "
                            "under RV means the reverse."
                        )


# ── Main layout ────────────────────────────────────────────────────────────────
st.title("OI Advanced Analytics")
st.caption(
    f"Old Date: **{old_date.strftime('%d %b %Y')}**  |  "
    f"New Date: **{new_date.strftime('%d %b %Y')}**  |  "
    f"OI Change + Volume → run `app.py`"
)

tabs = st.tabs([cm["tab_label"] for cm in c.COMMODITIES])

for tab, cm in zip(tabs, c.COMMODITIES):
    with tab:
        atm_val = atm_data.get(cm["key"])
        atm_label = cm["atm_fmt"](atm_val) if atm_val is not None else "—"
        fut_df = c.load_fut(cm["fut_name"])
        render_commodity_tab(
            df=dfs[cm["key"]], atm_val=atm_val, atm_label=atm_label,
            old_date=old_date, new_date=new_date,
            key_prefix=cm["key"].lower(), title=cm["title"],
            display_step=cm["display_step"], mround_default=cm["mround_default"],
            ingest_note=cm["ingest_note"], fut_df=fut_df,
        )
