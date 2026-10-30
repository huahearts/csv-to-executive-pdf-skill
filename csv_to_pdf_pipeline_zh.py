#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic CSV-to-Executive-PDF Pipeline (Production v2.2 - Chinese/CJK Edition).

Fixes all 8 strict engineering audit findings:
1. Strict --input vs --demo separation: raises FileNotFoundError immediately if --input does not exist.
2. Strict numeric & boolean parsing: strips currency formatting, raises ValueError on unparseable numbers, and maps boolean strings (yes/no/true/false/是/否/已退款/未退款/1/0).
3. Explicit accounting columns: requires fee and refund columns unless --estimate-missing-fees or --assume-zero-refunds is explicitly passed.
4. Semantic period sorting: parses ISO weeks (2026-W9 -> (2026, 9)), months, quarters, and dates.
5. Consistent PoP delta state: sets wow_net_delta = None when prior period <= 0 or single period, ensuring JSON, table, and prose match.
6. Hard exit code on PDF failure: raises RuntimeError and exits with code 2 if Chromium PDF compilation fails.
7. Programmatic page-count verification & Top-N aggregation (--max-table-rows 6, --max-pages 1).
8. Cross-platform CJK font support (Linux, macOS, Windows).
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

BANNED_AI_WORDS_ZH = [
    "赋能", "抓手", "瞬息万变", "总而言之", "不言而喻", "众所周知",
    "颠覆性", "完美无瑕", "里程碑式", "全方位", "深入剖析", "画卷"
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
        raise ValueError(f"金额列 '{col_name}' 在以下行索引存在空值 (NaN): {bad_idx}。")

    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"^[$€£¥,\s]+|[,\s]+$", "", regex=True)
        .str.replace(",", "", regex=False)
    )
    num = pd.to_numeric(cleaned, errors="coerce")
    if num.isna().any():
        bad_rows = series[num.isna()]
        examples = [f"行 {idx}: {val!r}" for idx, val in bad_rows.head(5).items()]
        raise ValueError(
            f"金额列 '{col_name}' 存在无法解析的非数字内容 ({', '.join(examples)})。"
            f"为保障财务数据准确性，系统已禁止将脏数据静默归零。"
        )
    num = num.astype(float)
    invalid = ~np.isfinite(num)
    if invalid.any():
        raise ValueError(f"Column {col_name!r} contains non-finite values at row indices {num.index[invalid].tolist()[:5]}.")
    return num


def parse_strict_refund_series(series: pd.Series, col_name: str) -> pd.Series:
    """Strictly parses boolean/binary refund column; supports 1/0, true/false, yes/no, 是/否, 已退款/未退款."""
    if series.isna().any():
        bad_idx = series[series.isna()].index.tolist()[:5]
        raise ValueError(f"退款标记列 '{col_name}' 在以下行索引存在空值 (NaN): {bad_idx}。")

    mapping = {
        "1": 1, "1.0": 1, "true": 1, "yes": 1, "y": 1, "refunded": 1, "是": 1, "已退款": 1, "退款": 1,
        "0": 0, "0.0": 0, "false": 0, "no": 0, "n": 0, "paid": 0, "completed": 0, "否": 0, "未退款": 0, "正常": 0,
    }
    norm = series.astype(str).str.strip().str.lower()
    mapped = norm.map(mapping)
    if mapped.isna().any():
        bad_rows = series[mapped.isna()]
        examples = [f"行 {idx}: {val!r}" for idx, val in bad_rows.head(5).items()]
        raise ValueError(
            f"退款标记列 '{col_name}' 存在无法识别的值 ({', '.join(examples)})。"
            f"允许值: 0/1, true/false, yes/no, y/n, refunded/paid, 是/否, 已退款/未退款。"
        )
    return mapped.astype(int)


def generate_mock_raw_csv_zh(csv_path: str, num_rows: int = 4200) -> pd.DataFrame:
    """Generates a deterministic 4,200-row Chinese SaaS/E-commerce transaction CSV when --demo is passed."""
    np.random.seed(42)
    weeks = [f"2026-W{w:02d}" for w in range(35, 43)]
    channels = ["企业年度订阅", "专业版月度订阅", "自助基础版", "API用量加油包", "渠道合伙人分销"]
    regions = ["北美市场 (US/CA)", "欧洲市场 (EU/UK)", "日本市场 (JP)", "亚太及东南亚 (SG/HK)"]

    rows = []
    for i in range(1, num_rows + 1):
        wk = np.random.choice(weeks, p=[0.10, 0.11, 0.12, 0.12, 0.13, 0.13, 0.14, 0.15])
        ch = np.random.choice(channels, p=[0.18, 0.35, 0.27, 0.12, 0.08])
        reg = np.random.choice(regions, p=[0.48, 0.24, 0.18, 0.10])

        base_price = {
            "企业年度订阅": 1200.0,
            "专业版月度订阅": 79.0,
            "自助基础版": 29.0,
            "API用量加油包": 45.0,
            "渠道合伙人分销": 99.0,
        }[ch]

        gross = round(base_price * np.random.uniform(0.85, 1.35), 2)
        is_refund = 1 if (ch == "自助基础版" and wk == "2026-W42" and np.random.rand() < 0.14) else (
            1 if np.random.rand() < 0.025 else 0
        )
        fee = round(gross * 0.029 + 0.30, 2)
        net = round(0.0 if is_refund else (gross - fee), 2)

        rows.append({
            "订单号": f"ORD-{100000 + i}",
            "结算周期": wk,
            "业务渠道": ch,
            "销售大区": reg,
            "总流水_USD": gross,
            "通道手续费_USD": fee,
            "是否退款": is_refund,
            "净营收_USD": net,
        })

    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8")
    return df


def load_and_validate_csv_zh(args: argparse.Namespace) -> tuple[pd.DataFrame, str]:
    """Strictly validates input path, required columns, and data types without silent assumptions."""
    if args.input:
        if not os.path.isfile(args.input):
            raise FileNotFoundError(
                f"指定的输入文件不存在: '{args.input}'。"
                f"（如需生成模拟演示数据，请去掉 --input 并显式传入 --demo 参数。）"
            )
        raw_df = pd.read_csv(args.input)
        source_path = args.input
    elif args.demo:
        source_path = args.demo_csv_out
        print(f"[DEMO MODE] 正在生成 4,200 行中文模拟演示数据: {source_path}")
        raw_df = generate_mock_raw_csv_zh(source_path, 4200)
    else:
        raise ValueError("必须指定 '--input <CSV路径>' 处理真实数据，或显式传入 '--demo' 运行模拟演示。")

    if len(raw_df) == 0:
        raise ValueError(f"输入 CSV 文件为空（0 行）: '{source_path}'")

    base_required = [args.col_id, args.col_period, args.col_channel, args.col_region, args.col_gross]
    missing_base = [c for c in base_required if c not in raw_df.columns]
    if missing_base:
        raise ValueError(f"CSV 缺少必要基础列: {missing_base}。当前可用列: {list(raw_df.columns)}")

    df = pd.DataFrame()
    df["订单号"] = raw_df[args.col_id].astype(str).str.strip()
    df["结算周期"] = raw_df[args.col_period].astype(str).str.strip()
    df["业务渠道"] = raw_df[args.col_channel].astype(str).str.strip()
    df["销售大区"] = raw_df[args.col_region].astype(str).str.strip()

    try:
        parsed = [parse_period_sort_key(v) for v in df["结算周期"]]
        df["结算周期"] = [item[0] for item in parsed]
        df["_sort_key"] = [item[1] for item in parsed]
    except ValueError as exc:
        raise ValueError(f"周期列 '{args.col_period}' 校验失败: {exc}") from exc

    df["总流水_USD"] = parse_strict_numeric_series(raw_df[args.col_gross], args.col_gross)

    if args.col_fee in raw_df.columns:
        df["通道手续费_USD"] = parse_strict_numeric_series(raw_df[args.col_fee], args.col_fee)
    elif args.estimate_missing_fees:
        print(f"[WARN] 缺少手续费列 '{args.col_fee}'，已按 --estimate-missing-fees 显式授权估算 (2.9% + $0.30)。")
        df["通道手续费_USD"] = (df["总流水_USD"] * 0.029 + 0.30).round(2)
    else:
        raise ValueError(
            f"CSV 缺少手续费列 '{args.col_fee}'。请通过 '--col-fee' 指定列名，或显式传入 '--estimate-missing-fees' 启用估算。"
        )

    if args.col_refund in raw_df.columns:
        df["是否退款"] = parse_strict_refund_series(raw_df[args.col_refund], args.col_refund)
    elif args.assume_zero_refunds:
        print(f"[WARN] 缺少退款列 '{args.col_refund}'，已按 --assume-zero-refunds 显式授权默认未退款。")
        df["是否退款"] = 0
    else:
        raise ValueError(
            f"CSV 缺少退款标记列 '{args.col_refund}'。请通过 '--col-refund' 指定列名，或显式传入 '--assume-zero-refunds'。"
        )

    if args.col_net and args.col_net in raw_df.columns:
        df["净营收_USD"] = parse_strict_numeric_series(raw_df[args.col_net], args.col_net)
    elif args.derive_net_from_gross or not args.col_net:
        df["净营收_USD"] = np.where(df["是否退款"] == 1, 0.0, df["总流水_USD"] - df["通道手续费_USD"]).round(2)
    else:
        raise ValueError(
            f"CSV 缺少净营收列 '{args.col_net}'。请传入 '--derive-net-from-gross' 允许按 (未退款时总流水 - 手续费) 推算。"
        )

    return df, source_path


def _aggregate_top_n_zh(records: list[dict], key_name: str, max_rows: int = 6) -> list[dict]:
    """Caps table rows to max_rows by grouping tail into '其他汇总 (N 项)' to guarantee 1-page fit."""
    if len(records) <= max_rows:
        return records
    head = records[: max_rows - 1]
    tail = records[max_rows - 1 :]
    other_txns = sum(r["txns"] for r in tail)
    other_net = round(sum(r["net"] for r in tail), 2)
    other_share = round(sum(r["share_pct"] for r in tail), 1)
    other_row = {
        key_name: f"其他汇总 ({len(tail)} 项)",
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
        other_row["status"] = "⚠ 尾部渠道含退款异常" if any_anom else "✓ 尾部渠道正常汇总"
        other_row["is_anomaly"] = any_anom
    return head + [other_row]


def compute_metrics_zh(df: pd.DataFrame, refund_threshold: float = 3.5, max_table_rows: int = 6) -> dict:
    """Computes 100% deterministic metrics with chronological sorting and consistent PoP states."""
    total_rows = int(len(df))
    gross_total = float(df["总流水_USD"].sum())
    net_total = float(df["净营收_USD"].sum())
    fee_total = float(df["通道手续费_USD"].sum())
    net_realization_rate_pct = round((net_total / gross_total * 100.0), 1) if gross_total > 0 else 0.0
    refund_rate = float(df["是否退款"].mean() * 100.0)
    aov = float(gross_total / total_rows) if total_rows > 0 else 0.0

    weekly = df.groupby("结算周期", as_index=False).agg(
        sort_key=("_sort_key", "first"),
        txns=("订单号", "count"),
        gross=("总流水_USD", "sum"),
        net=("净营收_USD", "sum"),
        refund_pct=("是否退款", lambda x: round(float(x.mean() * 100.0), 2)),
    ).sort_values(by="sort_key").drop(columns=["sort_key"]).reset_index(drop=True)

    latest_week = str(weekly.iloc[-1]["结算周期"])
    latest_week_net = round(float(weekly.iloc[-1]["net"]), 2)

    if len(weekly) < 2:
        wow_net_delta = None
        wow_delta_str = f"不可计算 (单周期: {latest_week})"
        delta_prose = f"（当前仅含单周期 {latest_week} 数据，环比不可计算）"
    else:
        prev_week = str(weekly.iloc[-2]["结算周期"])
        w_prev_net = float(weekly.iloc[-2]["net"])
        if w_prev_net <= 0:
            wow_net_delta = None
            wow_delta_str = f"不可计算 (上期 {prev_week} <= $0)"
            delta_prose = f"（{latest_week} 当期净营收 ${latest_week_net:,.2f}；因上期 {prev_week} 净营收为 ${w_prev_net:,.2f}，环比百分比不可计算）"
        else:
            wow_net_delta = round(((latest_week_net - w_prev_net) / w_prev_net) * 100.0, 1)
            wow_delta_str = f"{'+' if wow_net_delta >= 0 else ''}{wow_net_delta}% ({latest_week} 对比 {prev_week})"
            delta_prose = f"带动 {latest_week} 单周期净营收较 {prev_week} 环比变动 {wow_net_delta:+.1f}%"

    df_latest = df[df["结算周期"].astype(str) == latest_week]
    latest_stats = df_latest.groupby("业务渠道")["是否退款"].agg(["count", "sum"])
    latest_ch_refunds = (latest_stats["sum"] / latest_stats["count"] * 100).round(2).to_dict()
    latest_counts = latest_stats["count"].to_dict()
    latest_refunded = latest_stats["sum"].to_dict()

    by_channel = df.groupby("业务渠道", as_index=False).agg(
        txns=("订单号", "count"),
        refunded_txns=("是否退款", "sum"),
        gross=("总流水_USD", "sum"),
        net=("净营收_USD", "sum"),
        refund_pct=("是否退款", lambda x: round(float(x.mean() * 100.0), 2)),
    ).sort_values(by="net", ascending=False).reset_index(drop=True)

    by_channel["share_pct"] = np.where(net_total > 0, (by_channel["net"] / net_total * 100.0).round(1), 0.0)
    by_channel["latest_refund_pct"] = by_channel["业务渠道"].map(lambda c: latest_ch_refunds.get(c, 0.0))

    channel_records_full = []
    anomalies = []
    for _, r in by_channel.iterrows():
        c_name = str(r["业务渠道"])
        cum_ref = float(r["refund_pct"])
        lat_ref = float(r["latest_refund_pct"])
        is_anomaly = (cum_ref > refund_threshold) or (lat_ref > refund_threshold * 1.5)
        if is_anomaly:
            status_str = f"⚠ 异常预警：{latest_week}当期退款率 {lat_ref}% (累计 {cum_ref}%)"
            anomalies.append({
                "业务渠道": c_name,
                "cumulative_refund_pct": cum_ref,
                "latest_period_refund_pct": lat_ref,
                "period": latest_week,
                "threshold_pct": refund_threshold,
            })
        else:
            status_str = f"✓ 正常受控 (<{refund_threshold}%)"

        channel_records_full.append({
            "业务渠道": c_name,
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

    by_region = df.groupby("销售大区", as_index=False).agg(
        txns=("订单号", "count"),
        net=("净营收_USD", "sum"),
    ).sort_values(by="net", ascending=False).reset_index(drop=True)
    by_region["share_pct"] = np.where(net_total > 0, (by_region["net"] / net_total * 100.0).round(1), 0.0)

    region_records_full = [
        {
            "销售大区": str(r["销售大区"]),
            "txns": int(r["txns"]),
            "net": round(float(r["net"]), 2),
            "share_pct": float(r["share_pct"]),
        }
        for _, r in by_region.iterrows()
    ]

    top_ch = channel_records_full[0]
    bullet_1 = (
        f"核心营收支柱 — {top_ch['业务渠道']}：凭 {top_ch['txns']:,} 笔订单贡献了 "
        f"${top_ch['net']:,.2f} 净营收（占总盘 {top_ch['share_pct']}%，支付净额留存率（扣除手续费与退款后 {net_realization_rate_pct}%）），{delta_prose}。"
    )

    if anomalies:
        worst = sorted(anomalies, key=lambda x: x["latest_period_refund_pct"], reverse=True)[0]
        bullet_2 = (
            f"阈值熔断预警 — {worst['业务渠道']}退款率异动：自动化审计捕获该渠道在 {worst['period']} "
            f"当期退款率达 {worst['latest_period_refund_pct']}%（累计退款率 {worst['cumulative_refund_pct']}%，超出 {refund_threshold}% 预设红线）。"
            f"鉴于交易流水表不含故障日志，建议联动排查近期客服工单与支付网关日志以定位具体原因。"
        )
    else:
        max_ref_ch = sorted(channel_records_full, key=lambda x: x["refund_pct"], reverse=True)[0]
        bullet_2 = (
            f"退款率健康受控：全部业务渠道累计退款率均低于 {refund_threshold}% 预警阈值"
            f"（当前最高为“{max_ref_ch['业务渠道']}”的 {max_ref_ch['refund_pct']}%）。"
        )

    if len(region_records_full) >= 2:
        r1, r2 = region_records_full[0], region_records_full[1]
        combined_share = round(r1["share_pct"] + r2["share_pct"], 1)
        bullet_3 = (
            f"区域营收集中度：{r1['销售大区']}（${r1['net']:,.2f}，占 {r1['share_pct']}%）与 "
            f"{r2['销售大区']}（${r2['net']:,.2f}，占 {r2['share_pct']}%）合计贡献了 {combined_share}% 的净营收总额。"
        )
    else:
        r1 = region_records_full[0]
        bullet_3 = f"区域营收分布：{r1['销售大区']} 贡献了 ${r1['net']:,.2f}（占比 {r1['share_pct']}%）。"

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
        "by_channel": _aggregate_top_n_zh(channel_records_full, "业务渠道", max_table_rows),
        "by_region": _aggregate_top_n_zh(region_records_full, "销售大区", max_table_rows),
        "total_distinct_channels": len(channel_records_full),
        "total_distinct_regions": len(region_records_full),
    }


def lint_executive_copy_zh(bullets: list[str]) -> list[str]:
    violations = []
    for idx, text in enumerate(bullets, start=1):
        for word in BANNED_AI_WORDS_ZH:
            if word in text:
                violations.append(f"第 {idx} 条摘要包含禁用套话词汇 '{word}'")
    if violations:
        raise ValueError(f"[LINTER FAIL] 检测到 AI 塑料感禁用词: {violations}")
    print(f"[LINTER PASS] 已完成 {len(bullets)} 条高管摘要去 AI 腔调扫描（0 违规词）。")
    return violations


def render_charts_zh(metrics: dict, chart_path: str) -> None:
    plt.rcParams["font.sans-serif"] = [
        "Noto Sans CJK SC", "Noto Sans CJK JP", "PingFang SC", "Hiragino Sans GB",
        "Microsoft YaHei", "SimHei", "Inter", "DejaVu Sans"
    ]
    plt.rcParams["axes.unicode_minus"] = False

    weekly = pd.DataFrame(metrics["weekly"])
    by_ch = pd.DataFrame(metrics["by_channel"])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.5, 2.15), dpi=200)

    ax1.plot(weekly["结算周期"], weekly["net"] / 1000.0, marker="o", linewidth=2.5, color=PRIMARY)
    ax1.fill_between(weekly["结算周期"], weekly["net"] / 1000.0, color=PRIMARY, alpha=0.08)
    ax1.set_title("周期净营收增长趋势 (单位: 千美元 $K)", fontsize=10, fontweight="bold", color=PRIMARY, pad=8)
    ax1.set_ylabel("净营收 ($K)", fontsize=8.5, color=PRIMARY)
    ax1.tick_params(axis="x", rotation=25, labelsize=8)
    ax1.tick_params(axis="y", labelsize=8)
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    ax1.yaxis.grid(True, linestyle="--", alpha=0.4)

    for idx, row in weekly.iterrows():
        if idx in [0, len(weekly) - 1]:
            ax1.annotate(
                f"${row['net']/1000:.1f}K",
                (row["结算周期"], row["net"] / 1000.0),
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
    ax2.set_yticklabels([str(c)[:16] for c in by_ch["业务渠道"]], fontsize=8.5, fontweight="bold", color=PRIMARY)
    ax2.invert_yaxis()
    max_val = max(float(by_ch["net"].max()) / 1000.0, 1.0)
    ax2.set_xlim(0, max_val * 1.30)
    ax2.set_title("各业务渠道净营收贡献占比 ($K)", fontsize=10, fontweight="bold", color=PRIMARY, pad=8)
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


def build_pdf_html_zh(metrics: dict, chart_path: str, html_path: str) -> None:
    with open(chart_path, "rb") as f:
        chart_b64 = base64.b64encode(f.read()).decode("ascii")

    ch_rows = ""
    for r in metrics["by_channel"]:
        ch_rows += f"""
        <tr>
          <td><strong>{r['业务渠道']}</strong></td>
          <td style="text-align:right;">{r['txns']:,} 笔</td>
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
          <td><strong>{r['销售大区']}</strong></td>
          <td style="text-align:right;">{r['txns']:,} 笔</td>
          <td style="text-align:right;"><strong>${r['net']:,.2f}</strong></td>
          <td style="text-align:right;">{r['share_pct']}%</td>
        </tr>"""

    bullets_html = "\n".join(f"      <li>{b}</li>" for b in metrics["executive_bullets"])

    html = f"""<!DOCTYPE html>
<html lang="zh-Hans">
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
        content: "由 Deliverable-First AI Skill v2.2 自动生成 | 第 " counter(page) " 页";
        font-family: 'Noto Sans CJK SC', 'PingFang SC', 'Microsoft YaHei', 'Inter', sans-serif;
        font-size: 8pt;
        color: #64748B;
      }}
    }}
    *, *::before, *::after {{ box-sizing: border-box; border: none; border-radius: 0; box-shadow: none; background: transparent; }}
    html, body {{ margin: 0; padding: 0; font-family: 'Inter', 'Noto Sans CJK SC', 'PingFang SC', 'Microsoft YaHei', sans-serif; font-size: 9.5pt; line-height: 1.45; color: var(--text); }}
    .full-bleed-header {{ background: var(--primary); color: #fff; padding: 10px 18px; margin-bottom: 8px; }}
    .full-bleed-header h1 {{ color: #fff; font-size: 15pt; margin: 0 0 4px 0; font-weight: 800; }}
    .full-bleed-header p {{ color: #CBD5E1; font-size: 9pt; margin: 0; }}
    h2 {{ font-size: 11pt; color: var(--primary); margin: 8px 0 4px 0; font-weight: 700; break-after: avoid; }}
    p, ul {{ margin: 0 0 8px 0; }}
    ul {{ padding-left: 18px; }}
    li {{ margin-bottom: 4px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 4px 0 8px 0; }}
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
      <h1>周度经营与渠道利润自动审计高管简报 ({metrics['latest_week']})</h1>
      <p>自动化渲染管线 v2.2：Pandas 确定性核算 + Chromium 矢量排版 | 已核验记录：{metrics['total_rows']:,} 行 | 支付净额留存率（扣除手续费与退款后：{metrics['net_realization_rate_pct']}%）</p>
    </div>

    <p>支付净额留存率 = 净营收 ÷ 总流水；通常反映手续费及退款后的留存，不包含运营费用、销售成本或税费。若净额由 CSV 提供，其口径取决于数据源。</p>
    <h2>1. 核心财务与运营指标总览（Pandas 100% 确定性计算 — 零大模型算术幻觉）</h2>
    <table>
      <thead>
        <tr>
          <th>累计核算订单数</th>
          <th style="text-align:right;">总流水金额 (Gross)</th>
          <th style="text-align:right;">支付通道手续费</th>
          <th style="text-align:right;">实际净营收 (Net)</th>
          <th style="text-align:right;">平均客单价 (AOV)</th>
          <th style="text-align:right;">最新周期净增幅</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td><strong>{metrics['total_rows']:,} 笔</strong></td>
          <td style="text-align:right;">${metrics['gross_total']:,.2f}</td>
          <td style="text-align:right;">${metrics['fee_total']:,.2f}</td>
          <td style="text-align:right;"><strong>${metrics['net_total']:,.2f}</strong></td>
          <td style="text-align:right;">${metrics['aov']:,.2f}</td>
          <td style="text-align:right;"><strong>{metrics['wow_delta_str']}</strong></td>
        </tr>
      </tbody>
    </table>

    <h2>2. 数据驱动高管归因摘要与异常审计（已通过代码级去 AI 塑料感正则扫描）</h2>
    <ul>
{bullets_html}
    </ul>

    <figure>
      <img src="data:image/png;base64,{chart_b64}" alt="周度趋势与渠道分布图">
    </figure>

    <h2>3. 业务渠道明细与自动化利润审计表</h2>
    <table>
      <thead>
        <tr>
          <th>业务渠道名称</th>
          <th style="text-align:right;">成交笔数</th>
          <th style="text-align:right;">总流水 (USD)</th>
          <th style="text-align:right;">净营收 (USD)</th>
          <th style="text-align:right;">净营收占比</th>
          <th style="text-align:right;">累计退款率</th>
          <th>脚本自动审计状态</th>
        </tr>
      </thead>
      <tbody>
        {ch_rows}
      </tbody>
    </table>

    <h2>4. 全球销售大区净营收分布</h2>
    <table>
      <thead>
        <tr>
          <th>销售大区</th>
          <th style="text-align:right;">有效订单数</th>
          <th style="text-align:right;">净营收总额 (USD)</th>
          <th style="text-align:right;">大区营收占比</th>
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
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome import options as chrome_options
        import pypdfium2 as pdfium
    except ImportError as exc:
        if allow_html_only:
            print(f"[WARN] PDF 依赖缺失 ({exc})；已启用 --allow-html-only，保留 HTML: {html_path}")
            return False
        raise RuntimeError(f"PDF 编译失败，缺少依赖 ({exc})。请安装 selenium 与 pypdfium2，或传入 --allow-html-only。") from exc

    user_data_dir = tempfile.mkdtemp(prefix="chromium_pdf_zh_")
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
            print(f"[WARN] Headless Chromium 编译跳过 ({exc})；已启用 --allow-html-only，保留 HTML: {html_path}")
            return False
        raise RuntimeError(f"Headless Chromium PDF 编译失败: {exc}。请确认已安装 Chrome/Chromium，或传入 --allow-html-only。") from exc
    finally:
        if driver is not None:
            driver.quit()
        shutil.rmtree(user_data_dir, ignore_errors=True)

    pdf_doc = pdfium.PdfDocument(pdf_path)
    num_pages = len(pdf_doc)
    pdf_doc.close()

    if max_pages > 0 and num_pages > max_pages:
        raise RuntimeError(f"页数预算校验失败: 生成的 PDF '{pdf_path}' 共 {num_pages} 页，超出 --max-pages={max_pages} 限制。")
    print(f"[PDF VERIFIED] 中文矢量 A4 PDF 已保存: {pdf_path} ({os.path.getsize(pdf_path):,} bytes, 共 {num_pages} 页, 校验通过)")
    return True


def _load_cjk_font(size: int) -> ImageFont.FreeTypeFont:
    cjk_font_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for fp in cjk_font_paths:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_x_card_zh(raw_csv_path: str, pdf_path: str, x_card_path: str, total_rows: int = 4200) -> bool:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    page_pil = pdf[0].render(scale=2.5).to_pil().convert("RGB")
    pdf.close()

    canvas_w, canvas_h = 1600, 960
    card = Image.new("RGB", (canvas_w, canvas_h), (15, 37, 55))
    draw = ImageDraw.Draw(card)

    font_title = _load_cjk_font(28)
    font_sub = _load_cjk_font(20)
    font_mono = _load_cjk_font(14)

    draw.text(
        (48, 24),
        f"{total_rows:,} 行原始订单 CSV 导出表  -->  Codex 确定性脚本 v2.2  -->  出版级 A4 高管周报 PDF",
        fill=(255, 255, 255),
        font=font_title,
    )

    left_box = (48, 86, 750, 912)
    draw.rectangle(left_box, fill=(15, 23, 42), outline=(71, 85, 105), width=2)
    draw.rectangle((48, 86, 750, 134), fill=(30, 41, 59))
    draw.text((68, 96), f"处理前 (BEFORE): {total_rows:,} 行杂乱 CSV 原始流水表", fill=(248, 113, 113), font=font_sub)

    with open(raw_csv_path, "r", encoding="utf-8") as f:
        lines = [f.readline().strip() for _ in range(36)]

    y_text = 150
    for idx, line in enumerate(lines):
        if not line:
            break
        display_line = (line[:48] + "...") if len(line) > 50 else line
        color = (148, 163, 184) if idx > 0 else (56, 189, 248)
        draw.text((64, y_text), display_line, fill=color, font=font_mono)
        y_text += 20
    remaining = max(0, total_rows - len(lines) + 1)
    draw.text((64, y_text + 6), f"... [其余 {remaining:,} 行未排版原始记录] ...", fill=(217, 93, 57), font=font_mono)

    right_box = (810, 86, 1552, 912)
    draw.rectangle(right_box, fill=(241, 245, 249), outline=(217, 93, 57), width=3)
    draw.rectangle((810, 86, 1552, 134), fill=(217, 93, 57))
    draw.text((830, 96), "处理后 (AFTER): 零误差 A4 高管简报 PDF (Pandas + Chromium)", fill=(255, 255, 255), font=font_sub)

    target_h = 748
    aspect = page_pil.width / page_pil.height
    target_w = int(target_h * aspect)
    pdf_thumb = page_pil.resize((target_w, target_h), Image.Resampling.LANCZOS)

    paste_x = 810 + (742 - target_w) // 2
    paste_y = 148
    card.paste(pdf_thumb, (paste_x, paste_y))

    os.makedirs(os.path.dirname(os.path.abspath(x_card_path)), exist_ok=True)
    card.save(x_card_path, quality=95)
    print(f"[CARD RENDERED] 中文前后对比图已保存: {x_card_path}")
    return True


def parse_args_zh() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic CSV-to-Executive-PDF Pipeline v2.2 (Chinese Edition)")
    parser.add_argument("--input", type=str, default="", help="输入现有 CSV 路径。")
    parser.add_argument("--demo", action="store_true", help="显式生成并运行 4,200 行中文模拟演示数据集。")
    parser.add_argument("--demo-csv-out", type=str, default="raw_orders_zh_4200_rows.csv", help="中文演示 CSV 输出路径。")
    parser.add_argument("--output-pdf", type=str, default="sample_executive_briefing_report_zh.pdf", help="输出 A4 PDF 路径。")
    parser.add_argument("--output-html", type=str, default="demo_exec_briefing_zh.html", help="输出 HTML 路径。")
    parser.add_argument("--output-json", type=str, default="summary_metrics_zh.json", help="输出核算指标 JSON 路径。")
    parser.add_argument("--output-card", type=str, default="post_01_before_after_demo_card_zh.png", help="输出前后对比图路径。")
    parser.add_argument("--refund-threshold", type=float, default=3.5, help="退款率预警阈值百分比（默认 3.5）。")
    parser.add_argument("--max-table-rows", type=int, default=6, help="明细表最大展示行数，超出部分聚合为‘其他汇总’（默认 6）。")
    parser.add_argument("--max-pages", type=int, default=1, help="PDF 最大允许页数限制（0=不限，默认 1 页）。")
    parser.add_argument("--allow-html-only", action="store_true", help="当本机无 Chromium 时允许仅输出 HTML 而不报错退出。")
    parser.add_argument("--estimate-missing-fees", action="store_true", help="缺少手续费列时允许按 2.9% + $0.30 显式估算。")
    parser.add_argument("--assume-zero-refunds", action="store_true", help="缺少退款列时允许显式默认未退款。")
    parser.add_argument("--derive-net-from-gross", action="store_true", help="缺少净营收列时允许按 (总流水 - 手续费) 推算。")
    parser.add_argument("--col-id", type=str, default="订单号", help="订单号列名。")
    parser.add_argument("--col-period", type=str, default="结算周期", help="统计周期列名。")
    parser.add_argument("--col-channel", type=str, default="业务渠道", help="业务渠道列名。")
    parser.add_argument("--col-region", type=str, default="销售大区", help="销售大区列名。")
    parser.add_argument("--col-gross", type=str, default="总流水_USD", help="总流水列名。")
    parser.add_argument("--col-fee", type=str, default="通道手续费_USD", help="手续费列名。")
    parser.add_argument("--col-refund", type=str, default="是否退款", help="是否退款列名。")
    parser.add_argument("--col-net", type=str, default="净营收_USD", help="净营收列名。")
    return parser.parse_args()


def main() -> None:
    args = parse_args_zh()
    t0 = time.time()
    try:
        df_norm, csv_source_path = load_and_validate_csv_zh(args)
        metrics_data = compute_metrics_zh(
            df_norm, refund_threshold=args.refund_threshold, max_table_rows=args.max_table_rows
        )

        os.makedirs(os.path.dirname(os.path.abspath(args.output_json)), exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as jf:
            json.dump(metrics_data, jf, indent=2, ensure_ascii=False)
        print(f"[METRICS VERIFIED] 已导出中文确定性核算指标 JSON: {args.output_json}")

        lint_executive_copy_zh(metrics_data["executive_bullets"])

        chart_file = os.path.join(os.path.dirname(os.path.abspath(args.output_html)), "demo_exec_chart_zh.png")
        render_charts_zh(metrics_data, chart_file)
        build_pdf_html_zh(metrics_data, chart_file, args.output_html)

        pdf_ok = compile_html_to_pdf_and_verify(
            args.output_html, args.output_pdf, max_pages=args.max_pages, allow_html_only=args.allow_html_only
        )
        if pdf_ok and args.output_card:
            render_x_card_zh(csv_source_path, args.output_pdf, args.output_card, total_rows=metrics_data["total_rows"])

        elapsed = round(time.time() - t0, 2)
        print(f"[SUCCESS] 中文端到端渲染管线校验完毕，耗时 {elapsed}s。")
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        sys.stderr.write(f"[PIPELINE ERROR] {err}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
