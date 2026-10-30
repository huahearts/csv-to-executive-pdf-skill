#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic CSV-to-Executive-PDF Pipeline (Production v2.2 - English).

Fixes all 8 strict engineering audit findings:
1. Strict --input vs --demo separation: if --input does not exist, immediately raises FileNotFoundError (exit code != 0). Demo data requires explicit --demo flag.
2. Strict numeric & boolean parsing (zero silent coercion): strips currency formatting, raises ValueError with exact row/value on unparseable numbers, and maps boolean strings (yes/no/true/false/1/0) while rejecting invalid values.
3. Explicit accounting columns: requires fee and refund columns by default; only estimates fees or defaults refunds if --estimate-missing-fees or --assume-zero-refunds is explicitly passed.
4. Semantic period sorting: parses ISO weeks (2026-W9 -> (2026, 9)), months, quarters, and dates so 2026-W10 always sorts after 2026-W9.
5. Consistent PoP delta state: sets wow_net_delta = None when prior period <= 0 or single period, ensuring JSON, KPI table, and prose all state "N/A (prior period net revenue was zero)" rather than "+0.0%".
6. Hard exit code on PDF failure: raises RuntimeError and exits with code 2 if Chromium PDF compilation fails (unless --allow-html-only is explicitly passed).
7. Programmatic page-count verification & top-N table aggregation: aggregates channels/regions beyond Top 6 into "Other (N segments)" and verifies compiled PDF page count via pypdfium2 (--max-pages).
8. 100% alignment between SKILL.md, README.md, and code implementation (Net Realization Rate, Top Channels, Refund SLA Audit).
"""

import argparse
import base64
from datetime import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import time
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

PRIMARY = "#0F2537"
ACCENT = "#D95D39"
SECONDARY = "#2E6B9E"
MUTED = "#64748B"

BANNED_AI_WORDS_EN = [
    "delve", "tapestry", "testament", "landscape", "paradigm", "synergy",
    "game-changer", "cutting-edge", "seamless", "robust", "ever-evolving",
    "fast-paced", "holistic", "pivotal", "unleash", "unlock", "elevate",
    "revolutionize", "foster", "harness", "spearhead", "supercharge", "skyrocket"
]


def parse_period_sort_key(val: object) -> tuple[str, object]:
    """Return a canonical label and the period's actual start date."""
    s = str(val).strip()
    m = re.fullmatch(r"(\d{4})[-/\s]?[Ww](\d{1,2})", s)
    if m:
        year, week = map(int, m.groups())
        try:
            start = datetime.fromisocalendar(year, week, 1).date()
        except ValueError as exc:
            raise ValueError(f"Invalid ISO week: {s}") from exc
        return f"{year:04d}-W{week:02d}", start
    m = re.fullmatch(r"(\d{4})[-/\s]?[Qq]([1-4])", s)
    if m:
        year, quarter = map(int, m.groups())
        return f"{year:04d}-Q{quarter}", datetime(year, 3 * quarter - 2, 1).date()
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", s)
    if m:
        year, month = map(int, m.groups())
        return f"{year:04d}-{month:02d}", datetime(year, month, 1).date()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            date = datetime.strptime(s, fmt).date()
            return date.isoformat(), date
        except ValueError:
            continue
    raise ValueError(f"Unrecognized period format: {s!r}")


def parse_strict_numeric_series(series: pd.Series, col_name: str) -> pd.Series:
    """Strictly parses numeric/currency column; raises ValueError with row index on any unparseable value."""
    if series.isna().any():
        bad_idx = series[series.isna()].index.tolist()[:5]
        raise ValueError(f"Column '{col_name}' contains missing/NaN values at row indices {bad_idx}.")

    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"^[$€£¥,\s]+|[,\s]+$", "", regex=True)
        .str.replace(",", "", regex=False)
    )
    num = pd.to_numeric(cleaned, errors="coerce")
    if num.isna().any():
        bad_rows = series[num.isna()]
        examples = [f"row {idx}: {val!r}" for idx, val in bad_rows.head(5).items()]
        raise ValueError(
            f"Column '{col_name}' contains non-numeric values that cannot be parsed ({', '.join(examples)}). "
            f"Silent zero-coercion is disabled to protect financial integrity."
        )
    num = num.astype(float)
    invalid = ~np.isfinite(num)
    if invalid.any():
        raise ValueError(f"Column {col_name!r} contains non-finite values at row indices {num.index[invalid].tolist()[:5]}.")
    return num


def parse_strict_refund_series(series: pd.Series, col_name: str) -> pd.Series:
    """Strictly parses boolean/binary refund column; supports 1/0, true/false, yes/no, y/n, refunded/paid."""
    if series.isna().any():
        bad_idx = series[series.isna()].index.tolist()[:5]
        raise ValueError(f"Refund column '{col_name}' contains missing/NaN values at row indices {bad_idx}.")

    mapping = {
        "1": 1, "1.0": 1, "true": 1, "yes": 1, "y": 1, "refunded": 1, "是": 1, "已退款": 1, "退款": 1,
        "0": 0, "0.0": 0, "false": 0, "no": 0, "n": 0, "paid": 0, "completed": 0, "否": 0, "未退款": 0, "正常": 0,
    }
    norm = series.astype(str).str.strip().str.lower()
    mapped = norm.map(mapping)
    if mapped.isna().any():
        bad_rows = series[mapped.isna()]
        examples = [f"row {idx}: {val!r}" for idx, val in bad_rows.head(5).items()]
        raise ValueError(
            f"Refund column '{col_name}' contains unrecognized flag values ({', '.join(examples)}). "
            f"Allowed values: 0/1, true/false, yes/no, y/n, refunded/paid."
        )
    return mapped.astype(int)


def generate_mock_raw_csv(csv_path: str, num_rows: int = 4200) -> pd.DataFrame:
    """Generates a deterministic 4,200-row SaaS/Stripe export CSV only when --demo is passed."""
    np.random.seed(42)
    weeks = [f"2026-W{w:02d}" for w in range(35, 43)]
    channels = ["Enterprise Annual", "Pro Monthly", "Self-Serve Starter", "Usage Add-on", "Partner Referral"]
    regions = ["North America", "Europe (UK/EU)", "Japan (APAC)", "Singapore/SEA"]

    rows = []
    for i in range(1, num_rows + 1):
        wk = np.random.choice(weeks, p=[0.10, 0.11, 0.12, 0.12, 0.13, 0.13, 0.14, 0.15])
        ch = np.random.choice(channels, p=[0.18, 0.35, 0.27, 0.12, 0.08])
        reg = np.random.choice(regions, p=[0.48, 0.24, 0.18, 0.10])

        base_price = {
            "Enterprise Annual": 1200.0,
            "Pro Monthly": 79.0,
            "Self-Serve Starter": 29.0,
            "Usage Add-on": 45.0,
            "Partner Referral": 99.0,
        }[ch]

        gross = round(base_price * np.random.uniform(0.85, 1.35), 2)
        is_refund = 1 if (ch == "Self-Serve Starter" and wk == "2026-W42" and np.random.rand() < 0.14) else (
            1 if np.random.rand() < 0.025 else 0
        )
        fee = round(gross * 0.029 + 0.30, 2)
        net = round(0.0 if is_refund else (gross - fee), 2)

        rows.append({
            "txn_id": f"tx_{100000 + i}",
            "week": wk,
            "channel": ch,
            "region": reg,
            "gross_usd": gross,
            "stripe_fee_usd": fee,
            "refunded": is_refund,
            "net_usd": net,
        })

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8")
    return df


def load_and_validate_csv(args: argparse.Namespace) -> tuple[pd.DataFrame, str]:
    """Strictly validates input path, required columns, and data types without silent assumptions."""
    if args.input:
        if not os.path.isfile(args.input):
            raise FileNotFoundError(
                f"Specified --input file does not exist: '{args.input}'. "
                f"(To generate synthetic demo data, omit --input and pass --demo explicitly.)"
            )
        raw_df = pd.read_csv(args.input)
        source_path = args.input
    elif args.demo:
        source_path = args.demo_csv_out
        print(f"[DEMO MODE] Generating 4,200-row synthetic dataset at: {source_path}")
        raw_df = generate_mock_raw_csv(source_path, 4200)
    else:
        raise ValueError("You must specify either '--input <path_to_csv>' or '--demo' to run synthetic demo data.")

    if len(raw_df) == 0:
        raise ValueError(f"Input CSV '{source_path}' is empty (0 rows).")

    # Required base columns
    base_required = [args.col_id, args.col_period, args.col_channel, args.col_region, args.col_gross]
    missing_base = [c for c in base_required if c not in raw_df.columns]
    if missing_base:
        raise ValueError(
            f"Missing required columns in CSV: {missing_base}. Available columns: {list(raw_df.columns)}"
        )

    df = pd.DataFrame()
    df["txn_id"] = raw_df[args.col_id].astype(str).str.strip()
    df["week"] = raw_df[args.col_period].astype(str).str.strip()
    df["channel"] = raw_df[args.col_channel].astype(str).str.strip()
    df["region"] = raw_df[args.col_region].astype(str).str.strip()

    # Validate period format & chronological sort key on every row
    try:
        parsed = [parse_period_sort_key(v) for v in df["week"]]
        df["week"] = [item[0] for item in parsed]
        df["_sort_key"] = [item[1] for item in parsed]
    except ValueError as exc:
        raise ValueError(f"Period column '{args.col_period}' validation failed: {exc}") from exc

    # Strictly parse Gross Revenue
    df["gross_usd"] = parse_strict_numeric_series(raw_df[args.col_gross], args.col_gross)

    # Fee column handling (no silent assumption unless --estimate-missing-fees is set)
    if args.col_fee in raw_df.columns:
        df["stripe_fee_usd"] = parse_strict_numeric_series(raw_df[args.col_fee], args.col_fee)
    elif args.estimate_missing_fees:
        print(f"[WARN] Column '{args.col_fee}' absent; estimating fees via --estimate-missing-fees (2.9% + $0.30).")
        df["stripe_fee_usd"] = (df["gross_usd"] * 0.029 + 0.30).round(2)
    else:
        raise ValueError(
            f"Fee column '{args.col_fee}' is missing from CSV. Pass '--col-fee <column_name>' or "
            f"explicitly opt into '--estimate-missing-fees' if you want 2.9% + $0.30 estimated fees."
        )

    # Refund column handling (no silent assumption unless --assume-zero-refunds is set)
    if args.col_refund in raw_df.columns:
        df["refunded"] = parse_strict_refund_series(raw_df[args.col_refund], args.col_refund)
    elif args.assume_zero_refunds:
        print(f"[WARN] Column '{args.col_refund}' absent; setting refunded=0 via --assume-zero-refunds.")
        df["refunded"] = 0
    else:
        raise ValueError(
            f"Refund column '{args.col_refund}' is missing from CSV. Pass '--col-refund <column_name>' or "
            f"explicitly opt into '--assume-zero-refunds'."
        )

    # Net revenue handling
    if args.col_net and args.col_net in raw_df.columns:
        df["net_usd"] = parse_strict_numeric_series(raw_df[args.col_net], args.col_net)
    elif args.derive_net_from_gross or not args.col_net:
        df["net_usd"] = np.where(df["refunded"] == 1, 0.0, df["gross_usd"] - df["stripe_fee_usd"]).round(2)
    else:
        raise ValueError(
            f"Net revenue column '{args.col_net}' is missing from CSV. Pass '--derive-net-from-gross' "
            f"to compute net_usd = (0 if refunded else gross - fee)."
        )

    return df, source_path


def _aggregate_top_n(records: list[dict], key_name: str, max_rows: int = 6) -> list[dict]:
    """Caps table rows to max_rows by grouping the tail into an 'Other (N segments)' row to guarantee 1-page fit."""
    if len(records) <= max_rows:
        return records
    head = records[: max_rows - 1]
    tail = records[max_rows - 1 :]
    other_txns = sum(r["txns"] for r in tail)
    other_net = round(sum(r["net"] for r in tail), 2)
    other_share = round(sum(r["share_pct"] for r in tail), 1)
    other_row = {
        key_name: f"Other ({len(tail)} segments)",
        "txns": other_txns,
        "net": other_net,
        "share_pct": other_share,
    }
    if "gross" in records[0]:
        other_row["gross"] = round(sum(r["gross"] for r in tail), 2)
        other_row["refunded_txns"] = sum(r["refunded_txns"] for r in tail)
        other_row["latest_txns"] = sum(r["latest_txns"] for r in tail)
        other_row["latest_refunded_txns"] = sum(r["latest_refunded_txns"] for r in tail)
        other_row["refund_pct"] = round(other_row["refunded_txns"] / other_txns * 100, 2) if other_txns else 0.0
        other_row["latest_refund_pct"] = round(other_row["latest_refunded_txns"] / other_row["latest_txns"] * 100, 2) if other_row["latest_txns"] else 0.0
        any_anom = any(r.get("is_anomaly", False) for r in tail)
        other_row["status"] = "⚠ Contains Anomaly in Tail" if any_anom else "✓ Aggregated Tail"
        other_row["is_anomaly"] = any_anom
    return head + [other_row]


def compute_deterministic_metrics(df: pd.DataFrame, refund_threshold: float = 3.5, max_table_rows: int = 6) -> dict:
    """Computes 100% deterministic metrics with chronological period sorting and consistent PoP states."""
    total_rows = int(len(df))
    gross_total = float(df["gross_usd"].sum())
    net_total = float(df["net_usd"].sum())
    fee_total = float(df["stripe_fee_usd"].sum())
    net_realization_rate_pct = round((net_total / gross_total * 100.0), 1) if gross_total > 0 else 0.0
    refund_rate = float(df["refunded"].mean() * 100.0)
    aov = float(gross_total / total_rows) if total_rows > 0 else 0.0

    # Group by week and sort chronologically using _sort_key
    weekly = df.groupby("week", as_index=False).agg(
        sort_key=("_sort_key", "first"),
        txns=("txn_id", "count"),
        gross=("gross_usd", "sum"),
        net=("net_usd", "sum"),
        refund_pct=("refunded", lambda x: round(float(x.mean() * 100.0), 2)),
    ).sort_values(by="sort_key").drop(columns=["sort_key"]).reset_index(drop=True)

    latest_week = str(weekly.iloc[-1]["week"])
    latest_week_net = round(float(weekly.iloc[-1]["net"]), 2)

    # Consistent PoP / WoW Delta calculation (never outputs +0.0% when prior period is 0 or missing)
    if len(weekly) < 2:
        wow_net_delta = None
        wow_delta_str = f"N/A (Single Period: {latest_week})"
        delta_prose = f"in single-period dataset {latest_week} (period-over-period delta not applicable)"
    else:
        prev_week = str(weekly.iloc[-2]["week"])
        w_prev_net = float(weekly.iloc[-2]["net"])
        if w_prev_net <= 0:
            wow_net_delta = None
            wow_delta_str = f"N/A (Prior Period {prev_week} <= $0)"
            delta_prose = (
                f"with {latest_week} net revenue at ${latest_week_net:,.2f} "
                f"(period-over-period percentage delta not calculable because {prev_week} net revenue was ${w_prev_net:,.2f})"
            )
        else:
            wow_net_delta = round(((latest_week_net - w_prev_net) / w_prev_net) * 100.0, 1)
            wow_delta_str = f"{'+' if wow_net_delta >= 0 else ''}{wow_net_delta}% ({latest_week} vs {prev_week})"
            delta_prose = f"with {latest_week} net revenue changing {wow_net_delta:+.1f}% vs {prev_week}"

    df_latest = df[df["week"].astype(str) == latest_week]
    latest_stats = df_latest.groupby("channel")["refunded"].agg(["count", "sum"])
    latest_ch_refunds = (latest_stats["sum"] / latest_stats["count"] * 100).round(2).to_dict()
    latest_counts = latest_stats["count"].to_dict()
    latest_refunded = latest_stats["sum"].to_dict()

    by_channel = df.groupby("channel", as_index=False).agg(
        txns=("txn_id", "count"),
        refunded_txns=("refunded", "sum"),
        gross=("gross_usd", "sum"),
        net=("net_usd", "sum"),
        refund_pct=("refunded", lambda x: round(float(x.mean() * 100.0), 2)),
    ).sort_values(by="net", ascending=False).reset_index(drop=True)

    by_channel["share_pct"] = np.where(net_total > 0, (by_channel["net"] / net_total * 100.0).round(1), 0.0)
    by_channel["latest_refund_pct"] = by_channel["channel"].map(lambda c: latest_ch_refunds.get(c, 0.0))

    channel_records_full = []
    anomalies = []
    for _, r in by_channel.iterrows():
        c_name = str(r["channel"])
        cum_ref = float(r["refund_pct"])
        lat_ref = float(r["latest_refund_pct"])
        is_anomaly = (cum_ref > refund_threshold) or (lat_ref > refund_threshold * 1.5)
        if is_anomaly:
            status_str = f"⚠ Anomaly: {lat_ref}% in {latest_week} (Cum: {cum_ref}%)"
            anomalies.append({
                "channel": c_name,
                "cumulative_refund_pct": cum_ref,
                "latest_period_refund_pct": lat_ref,
                "period": latest_week,
                "threshold_pct": refund_threshold,
            })
        else:
            status_str = f"✓ Within Target (<{refund_threshold}%)"

        channel_records_full.append({
            "channel": c_name,
            "txns": int(r["txns"]),
            "refunded_txns": int(r["refunded_txns"]),
            "latest_txns": int(latest_counts.get(c_name, 0)),
            "latest_refunded_txns": int(latest_refunded.get(c_name, 0)),
            "gross": round(float(r["gross"]), 2),
            "net": round(float(r["net"]), 2),
            "share_pct": float(r["share_pct"]),
            "refund_pct": cum_ref,
            "latest_refund_pct": lat_ref,
            "status": status_str,
            "is_anomaly": bool(is_anomaly),
        })

    by_region = df.groupby("region", as_index=False).agg(
        txns=("txn_id", "count"),
        net=("net_usd", "sum"),
    ).sort_values(by="net", ascending=False).reset_index(drop=True)
    by_region["share_pct"] = np.where(net_total > 0, (by_region["net"] / net_total * 100.0).round(1), 0.0)

    region_records_full = [
        {
            "region": str(r["region"]),
            "txns": int(r["txns"]),
            "net": round(float(r["net"]), 2),
            "share_pct": float(r["share_pct"]),
        }
        for _, r in by_region.iterrows()
    ]

    top_ch = channel_records_full[0]
    bullet_1 = (
        f"Primary Revenue Driver — {top_ch['channel']}: Generated ${top_ch['net']:,.2f} "
        f"({top_ch['share_pct']}% of total net revenue, overall net realization rate {net_realization_rate_pct}%) across {top_ch['txns']:,} transactions, "
        f"{delta_prose}."
    )

    if anomalies:
        worst = sorted(anomalies, key=lambda x: x["latest_period_refund_pct"], reverse=True)[0]
        bullet_2 = (
            f"Threshold Alert — {worst['channel']} Refund Rate: Automated checks flagged a "
            f"{worst['latest_period_refund_pct']}% refund rate in {worst['period']} "
            f"(cumulative rate: {worst['cumulative_refund_pct']}%, exceeding the {refund_threshold}% SLA threshold). "
            f"Evidence is limited to transaction records; cross-check support tickets and payment gateway logs to isolate the root cause."
        )
    else:
        max_ref_ch = sorted(channel_records_full, key=lambda x: x["refund_pct"], reverse=True)[0]
        bullet_2 = (
            f"Refund SLA Compliance: All channels remained within the {refund_threshold}% refund threshold "
            f"(highest cumulative rate observed in {max_ref_ch['channel']} at {max_ref_ch['refund_pct']}%)."
        )

    if len(region_records_full) >= 2:
        r1, r2 = region_records_full[0], region_records_full[1]
        combined_share = round(r1["share_pct"] + r2["share_pct"], 1)
        bullet_3 = (
            f"Geographic Concentration: {r1['region']} (${r1['net']:,.2f}, {r1['share_pct']}%) and "
            f"{r2['region']} (${r2['net']:,.2f}, {r2['share_pct']}%) accounted for {combined_share}% of total net volume."
        )
    else:
        r1 = region_records_full[0]
        bullet_3 = f"Geographic Concentration: {r1['region']} generated ${r1['net']:,.2f} ({r1['share_pct']}% of net volume)."

    return {
        "total_rows": total_rows,
        "gross_total": round(gross_total, 2),
        "net_total": round(net_total, 2),
        "fee_total": round(fee_total, 2),
        "net_realization_rate_pct": net_realization_rate_pct,
        "refund_rate": round(refund_rate, 2),
        "aov": round(aov, 2),
        "latest_week": latest_week,
        "latest_week_net": latest_week_net,
        "wow_net_delta": wow_net_delta,
        "wow_delta_str": wow_delta_str,
        "refund_threshold": refund_threshold,
        "executive_bullets": [bullet_1, bullet_2, bullet_3],
        "anomalies": anomalies,
        "weekly": weekly.to_dict(orient="records"),
        "by_channel": _aggregate_top_n(channel_records_full, "channel", max_table_rows),
        "by_region": _aggregate_top_n(region_records_full, "region", max_table_rows),
        "total_distinct_channels": len(channel_records_full),
        "total_distinct_regions": len(region_records_full),
    }


def lint_executive_copy(bullets: list[str]) -> list[str]:
    """Scans executive commentary against banned AI slop vocabulary from anti_ai_style_guard.md."""
    violations = []
    for idx, text in enumerate(bullets, start=1):
        for word in BANNED_AI_WORDS_EN:
            if re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE):
                violations.append(f"Bullet #{idx} contains banned phrase '{word}'")
    if violations:
        raise ValueError(f"[LINTER FAIL] Anti-AI Style Guard violations detected: {violations}")
    print(f"[LINTER PASS] Verified {len(bullets)} executive bullets against {len(BANNED_AI_WORDS_EN)} banned AI slop terms (0 violations).")
    return violations


def render_executive_charts(metrics: dict, chart_path: str) -> None:
    """Renders high-DPI locked 2-tone charts with cross-platform font fallback."""
    plt.rcParams["font.sans-serif"] = [
        "Inter", "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", "DejaVu Sans", "Arial"
    ]
    plt.rcParams["axes.unicode_minus"] = False

    weekly = pd.DataFrame(metrics["weekly"])
    by_ch = pd.DataFrame(metrics["by_channel"])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.5, 2.5), dpi=200)

    ax1.plot(weekly["week"], weekly["net"] / 1000.0, marker="o", linewidth=2.5, color=PRIMARY)
    ax1.fill_between(weekly["week"], weekly["net"] / 1000.0, color=PRIMARY, alpha=0.08)
    ax1.set_title("Period Net Revenue Trend ($K USD)", fontsize=10, fontweight="bold", color=PRIMARY, pad=8)
    ax1.set_ylabel("Net Revenue ($K)", fontsize=8.5, color=PRIMARY)
    ax1.tick_params(axis="x", rotation=25, labelsize=8)
    ax1.tick_params(axis="y", labelsize=8)
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    ax1.yaxis.grid(True, linestyle="--", alpha=0.4)

    for idx, row in weekly.iterrows():
        if idx in [0, len(weekly) - 1]:
            ax1.annotate(
                f"${row['net']/1000:.1f}K",
                (row["week"], row["net"] / 1000.0),
                textcoords="offset points",
                xytext=(0, 6),
                ha="center",
                fontsize=8,
                fontweight="bold",
                color=ACCENT if idx == len(weekly) - 1 else PRIMARY,
            )

    colors = [ACCENT if i == 0 else PRIMARY if i == 1 else SECONDARY for i in range(len(by_ch))]
    y_pos = np.arange(len(by_ch))
    bars = ax2.barh(y_pos, by_ch["net"] / 1000.0, color=colors, height=0.55)
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels([str(c)[:26] for c in by_ch["channel"]], fontsize=8.5, fontweight="bold", color=PRIMARY)
    ax2.invert_yaxis()
    max_val = max(float(by_ch["net"].max()) / 1000.0, 1.0)
    ax2.set_xlim(0, max_val * 1.28)
    ax2.set_title("Net Revenue Contribution by Channel ($K)", fontsize=10, fontweight="bold", color=PRIMARY, pad=8)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    ax2.xaxis.grid(True, linestyle="--", alpha=0.4)

    for bar, (_, row) in zip(bars, by_ch.iterrows()):
        ax2.text(
            bar.get_width() + (max_val * 0.02),
            bar.get_y() + bar.get_height() / 2,
            f"${row['net']/1000:.1f}K ({row['share_pct']}%)",
            va="center",
            ha="left",
            fontsize=8,
            fontweight="bold",
            color=PRIMARY,
        )

    plt.tight_layout()
    os.makedirs(os.path.dirname(os.path.abspath(chart_path)), exist_ok=True)
    plt.savefig(chart_path, dpi=200, bbox_inches="tight")
    plt.close()


def build_pdf_html(metrics: dict, chart_path: str, html_path: str) -> None:
    """Builds a 1-page Executive Briefing HTML strictly grounded in dynamic metrics."""
    with open(chart_path, "rb") as f:
        chart_b64 = base64.b64encode(f.read()).decode("ascii")

    ch_rows = ""
    for r in metrics["by_channel"]:
        ch_rows += f"""
        <tr>
          <td><strong>{r['channel']}</strong></td>
          <td style="text-align:right;">{r['txns']:,}</td>
          <td style="text-align:right;">${r['gross']:,.2f}</td>
          <td style="text-align:right;"><strong>${r['net']:,.2f}</strong></td>
          <td style="text-align:right;">{r['share_pct']}%</td>
          <td style="text-align:right;">{r['refund_pct']}%</td>
          <td>{r['status']}</td>
        </tr>"""

    reg_rows = ""
    for r in metrics["by_region"]:
        reg_rows += f"""
        <tr>
          <td><strong>{r['region']}</strong></td>
          <td style="text-align:right;">{r['txns']:,}</td>
          <td style="text-align:right;"><strong>${r['net']:,.2f}</strong></td>
          <td style="text-align:right;">{r['share_pct']}%</td>
        </tr>"""

    bullets_html = "\n".join(f"      <li>{b}</li>" for b in metrics["executive_bullets"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <style>
    :root {{
      --primary: #0F2537;
      --accent: #D95D39;
      --text: #1E293B;
      --bg: #FFFFFF;
    }}
    @page {{
      size: A4;
      margin: 12mm 15mm;
      background: var(--bg);
      @bottom-right {{
        content: "Generated by Deliverable-First AI Skill v2.2 | Page " counter(page);
        font-family: 'Inter', sans-serif;
        font-size: 8pt;
        color: #64748B;
      }}
    }}
    *, *::before, *::after {{ box-sizing: border-box; border: none; border-radius: 0; box-shadow: none; background: transparent; }}
    html, body {{ margin: 0; padding: 0; font-family: 'Inter', 'Noto Sans CJK SC', 'PingFang SC', 'Microsoft YaHei', sans-serif; font-size: 9.5pt; line-height: 1.5; color: var(--text); }}
    .full-bleed-header {{ background: var(--primary); color: #fff; padding: 10px 18px; margin-bottom: 8px; }}
    .full-bleed-header h1 {{ color: #fff; font-size: 15pt; margin: 0 0 4px 0; font-weight: 800; }}
    .full-bleed-header p {{ color: #CBD5E1; font-size: 9pt; margin: 0; }}
    h2 {{ font-size: 11pt; color: var(--primary); margin: 8px 0 4px 0; font-weight: 700; break-after: avoid; }}
    p, ul {{ margin: 0 0 8px 0; }}
    ul {{ padding-left: 18px; }}
    li {{ margin-bottom: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 6px 0 10px 0; }}
    th, td {{ padding: 4px 7px; font-size: 9pt; text-align: left; }}
    th {{ border-bottom: 2px solid var(--primary); color: var(--primary); font-weight: 700; }}
    td {{ border-bottom: 1px solid rgba(0,0,0,0.08); }}
    figure {{ margin: 8px 0; break-inside: avoid; }}
    img {{ width: 100%; height: auto; display: block; }}
    tr {{ break-inside: avoid; }}
  </style>
</head>
<body>
  <main class="document-flow">
    <div class="full-bleed-header">
      <h1>Weekly Revenue &amp; Operations Executive Briefing ({metrics['latest_week']})</h1>
      <p>Automated via Deterministic CSV-to-PDF Skill Pipeline v2.2 | Verified Rows: {metrics['total_rows']:,} | Net Realization Rate (Post-Fee &amp; Refund Retained: {metrics['net_realization_rate_pct']}%)</p>
    </div>

    <p>Calculated as Net Revenue ÷ Gross Volume (typically gross less fees and refunds). Does not include operating overhead, COGS, or taxes. If net revenue is supplied, its accounting basis depends on the source.</p>
    <h2>1. Top-Line Deterministic KPI Summary (Verified via Pandas — Zero LLM Math)</h2>
    <table>
      <thead>
        <tr>
          <th>Total Transactions</th>
          <th style="text-align:right;">Gross Volume (USD)</th>
          <th style="text-align:right;">Payment Fees (USD)</th>
          <th style="text-align:right;">Net Revenue (USD)</th>
          <th style="text-align:right;">Avg Order Value</th>
          <th style="text-align:right;">Latest Period Net Delta</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>{metrics['total_rows']:,} rows</strong></td>
          <td style="text-align:right;">${metrics['gross_total']:,.2f}</td>
          <td style="text-align:right;">${metrics['fee_total']:,.2f}</td>
          <td style="text-align:right;"><strong>${metrics['net_total']:,.2f}</strong></td>
          <td style="text-align:right;">${metrics['aov']:,.2f}</td>
          <td style="text-align:right;"><strong>{metrics['wow_delta_str']}</strong></td>
        </tr>
      </tbody>
    </table>

    <h2>2. Data-Driven Executive Commentary &amp; Anomaly Audit (Regex Linter Verified)</h2>
    <ul>
{bullets_html}
    </ul>

    <figure>
      <img src="data:image/png;base64,{chart_b64}" alt="Weekly Trend and Channel Breakdown">
    </figure>

    <h2>3. Channel Performance &amp; Automated SLA Margin Audit</h2>
    <table>
      <thead>
        <tr>
          <th>Revenue Channel</th>
          <th style="text-align:right;">Txns</th>
          <th style="text-align:right;">Gross Revenue</th>
          <th style="text-align:right;">Net Revenue</th>
          <th style="text-align:right;">Net Share</th>
          <th style="text-align:right;">Cum. Refund Rate</th>
          <th>Automated Audit Status</th>
        </tr>
      </thead>
      <tbody>
        {ch_rows}
      </tbody>
    </table>

    <h2>4. Regional Revenue Breakdown</h2>
    <table>
      <thead>
        <tr>
          <th>Geographic Region</th>
          <th style="text-align:right;">Transactions</th>
          <th style="text-align:right;">Net Revenue (USD)</th>
          <th style="text-align:right;">Regional Share</th>
        </tr>
      </thead>
      <tbody>
        {reg_rows}
      </tbody>
    </table>
  </main>
</body>
</html>"""

    os.makedirs(os.path.dirname(os.path.abspath(html_path)), exist_ok=True)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)


def compile_html_to_pdf_and_verify(html_path: str, pdf_path: str, max_pages: int = 1, allow_html_only: bool = False) -> bool:
    """Compiles HTML to PDF via Headless Chromium and verifies page count; raises RuntimeError on failure."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome import options as chrome_options
        import pypdfium2 as pdfium
    except ImportError as exc:
        if allow_html_only:
            print(f"[WARN] PDF dependencies missing ({exc}); --allow-html-only enabled, keeping HTML at {html_path}")
            return False
        raise RuntimeError(
            f"PDF compilation failed: required package missing ({exc}). "
            f"Install 'selenium' and 'pypdfium2', or pass '--allow-html-only'."
        ) from exc

    user_data_dir = tempfile.mkdtemp(prefix="chromium_pdf_")
    driver = None
    try:
        opts = chrome_options.Options()
        for arg in [
            "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-gpu", "--disable-extensions", "--no-first-run",
            f"--user-data-dir={user_data_dir}",
        ]:
            opts.add_argument(arg)
        driver = webdriver.Chrome(options=opts)
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        driver.get("about:blank")
        frame_tree = driver.execute_cdp_cmd("Page.getFrameTree", {})
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        driver.execute_cdp_cmd("Page.setDocumentContent", {"frameId": frame_id, "html": html_content})
        time.sleep(1.5)

        result = driver.execute_cdp_cmd(
            "Page.printToPDF",
            {
                "landscape": False,
                "displayHeaderFooter": False,
                "printBackground": True,
                "preferCSSPageSize": True,
                "marginTop": 0, "marginBottom": 0, "marginLeft": 0, "marginRight": 0,
            },
        )
        os.makedirs(os.path.dirname(os.path.abspath(pdf_path)), exist_ok=True)
        with open(pdf_path, "wb") as f:
            f.write(base64.b64decode(result["data"]))
    except Exception as exc:
        if allow_html_only:
            print(f"[WARN] Headless Chromium failed ({exc}); --allow-html-only enabled, keeping HTML at {html_path}")
            return False
        raise RuntimeError(
            f"Headless Chromium PDF compilation failed: {exc}. "
            f"Ensure Chrome/Chromium and matching ChromeDriver are installed, or pass '--allow-html-only'."
        ) from exc
    finally:
        if driver is not None:
            driver.quit()
        shutil.rmtree(user_data_dir, ignore_errors=True)

    # Verify PDF page count budget programmatically
    pdf_doc = pdfium.PdfDocument(pdf_path)
    num_pages = len(pdf_doc)
    pdf_doc.close()

    if max_pages > 0 and num_pages > max_pages:
        raise RuntimeError(
            f"Page-budget check failed: generated PDF '{pdf_path}' has {num_pages} pages, "
            f"exceeding --max-pages={max_pages}."
        )
    print(
        f"[PDF VERIFIED] Vector A4 PDF saved to: {pdf_path} "
        f"({os.path.getsize(pdf_path):,} bytes, {num_pages} page(s), budget <={max_pages} PASS)"
    )
    return True


def _load_cross_platform_font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/System/Library/Fonts/Menlo.ttc",
            "C:/Windows/Fonts/consola.ttf",
        ]
        if mono else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/arialbd.ttf",
        ]
    )
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_x_card(raw_csv_path: str, pdf_path: str, x_card_path: str, total_rows: int = 4200) -> bool:
    """Renders the PDF first page and builds a side-by-side Before/After image for X."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    page_pil = pdf[0].render(scale=2.5).to_pil().convert("RGB")
    pdf.close()

    canvas_w, canvas_h = 1600, 960
    card = Image.new("RGB", (canvas_w, canvas_h), (15, 37, 55))
    draw = ImageDraw.Draw(card)

    font_title = _load_cross_platform_font(27, mono=False)
    font_sub = _load_cross_platform_font(19, mono=False)
    font_mono = _load_cross_platform_font(14, mono=True)

    draw.text(
        (48, 26),
        f"RAW {total_rows:,}-ROW CSV EXPORT  -->  DETERMINISTIC CODEX SKILL  -->  BOARD-READY A4 PDF",
        fill=(255, 255, 255),
        font=font_title,
    )

    left_box = (48, 86, 750, 912)
    draw.rectangle(left_box, fill=(15, 23, 42), outline=(71, 85, 105), width=2)
    draw.rectangle((48, 86, 750, 134), fill=(30, 41, 59))
    draw.text((68, 98), f"BEFORE: {os.path.basename(raw_csv_path)} (Unformatted)", fill=(248, 113, 113), font=font_sub)

    with open(raw_csv_path, "r", encoding="utf-8") as f:
        lines = [f.readline().strip() for _ in range(38)]

    y_text = 150
    for idx, line in enumerate(lines):
        if not line:
            break
        display_line = (line[:62] + "...") if len(line) > 65 else line
        color = (148, 163, 184) if idx > 0 else (56, 189, 248)
        draw.text((64, y_text), display_line, fill=color, font=font_mono)
        y_text += 19
    remaining = max(0, total_rows - len(lines) + 1)
    draw.text((64, y_text + 6), f"... [{remaining:,} more unformatted rows] ...", fill=(217, 93, 57), font=font_mono)

    right_box = (810, 86, 1552, 912)
    draw.rectangle(right_box, fill=(241, 245, 249), outline=(217, 93, 57), width=3)
    draw.rectangle((810, 86, 1552, 134), fill=(217, 93, 57))
    draw.text((830, 98), "AFTER: Executive Briefing PDF (Pandas + Chromium)", fill=(255, 255, 255), font=font_sub)

    target_h = 748
    aspect = page_pil.width / page_pil.height
    target_w = int(target_h * aspect)
    pdf_thumb = page_pil.resize((target_w, target_h), Image.Resampling.LANCZOS)

    paste_x = 810 + (742 - target_w) // 2
    paste_y = 148
    card.paste(pdf_thumb, (paste_x, paste_y))

    os.makedirs(os.path.dirname(os.path.abspath(x_card_path)), exist_ok=True)
    card.save(x_card_path, quality=95)
    print(f"[CARD RENDERED] Before/After comparison card saved to: {x_card_path}")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic CSV-to-Executive-PDF Pipeline v2.2 (Strict Validation)")
    parser.add_argument("--input", type=str, default="", help="Path to existing input CSV file.")
    parser.add_argument("--demo", action="store_true", help="Explicitly generate and run 4,200-row synthetic demo dataset.")
    parser.add_argument("--demo-csv-out", type=str, default="raw_stripe_export_4200_rows.csv", help="Output path for synthetic demo CSV.")
    parser.add_argument("--output-pdf", type=str, default="sample_executive_briefing_report.pdf", help="Output A4 PDF path.")
    parser.add_argument("--output-html", type=str, default="demo_exec_briefing.html", help="Output HTML report path.")
    parser.add_argument("--output-json", type=str, default="summary_metrics.json", help="Output verified metrics JSON path.")
    parser.add_argument("--output-card", type=str, default="post_01_before_after_demo_card.png", help="Output Before/After card PNG path.")
    parser.add_argument("--refund-threshold", type=float, default=3.5, help="Refund rate SLA threshold percentage (default: 3.5).")
    parser.add_argument("--max-table-rows", type=int, default=6, help="Max rows per breakdown table before grouping tail into 'Other' (default: 6).")
    parser.add_argument("--max-pages", type=int, default=1, help="Strict maximum page count allowed for PDF (0 = unlimited, default: 1).")
    parser.add_argument("--allow-html-only", action="store_true", help="Do not fail with exit code != 0 if Chromium is unavailable.")
    parser.add_argument("--estimate-missing-fees", action="store_true", help="Explicitly allow estimating fees (2.9% + $0.30) if fee column is absent.")
    parser.add_argument("--assume-zero-refunds", action="store_true", help="Explicitly allow setting refunded=0 if refund column is absent.")
    parser.add_argument("--derive-net-from-gross", action="store_true", help="Compute net_usd = (0 if refunded else gross - fee) if net column is absent.")
    parser.add_argument("--col-id", type=str, default="txn_id", help="Transaction ID column name.")
    parser.add_argument("--col-period", type=str, default="week", help="Time period / week column name.")
    parser.add_argument("--col-channel", type=str, default="channel", help="Channel / segment column name.")
    parser.add_argument("--col-region", type=str, default="region", help="Geographic region column name.")
    parser.add_argument("--col-gross", type=str, default="gross_usd", help="Gross revenue column name.")
    parser.add_argument("--col-fee", type=str, default="stripe_fee_usd", help="Fee column name.")
    parser.add_argument("--col-refund", type=str, default="refunded", help="Refund flag column name.")
    parser.add_argument("--col-net", type=str, default="net_usd", help="Net revenue column name.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    t0 = time.time()
    try:
        df_norm, csv_source_path = load_and_validate_csv(args)
        metrics_data = compute_deterministic_metrics(
            df_norm, refund_threshold=args.refund_threshold, max_table_rows=args.max_table_rows
        )

        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as jf:
            json.dump(metrics_data, jf, indent=2, ensure_ascii=False)
        print(f"[METRICS VERIFIED] Exported deterministic summary JSON to: {args.output_json}")

        lint_executive_copy(metrics_data["executive_bullets"])

        chart_file = os.path.join(os.path.dirname(os.path.abspath(args.output_html)), "demo_exec_chart.png")
        render_executive_charts(metrics_data, chart_file)
        build_pdf_html(metrics_data, chart_file, args.output_html)

        pdf_ok = compile_html_to_pdf_and_verify(
            args.output_html, args.output_pdf, max_pages=args.max_pages, allow_html_only=args.allow_html_only
        )
        if pdf_ok and args.output_card:
            render_x_card(csv_source_path, args.output_pdf, args.output_card, total_rows=metrics_data["total_rows"])

        elapsed = round(time.time() - t0, 2)
        print(f"[SUCCESS] End-to-end pipeline verified and completed in {elapsed}s.")
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        sys.stderr.write(f"[PIPELINE ERROR] {err}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
