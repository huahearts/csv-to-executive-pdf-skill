"""Shared reporting for observed net amounts and explicitly gross-only sales."""

import base64
import html
import math
import textwrap
from decimal import Decimal
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

CURRENCIES = {"USD": "$", "GBP": "£", "EUR": "€", "CNY": "¥", "JPY": "¥"}


def validate_money_precision(series, column):
    """Require exact hundredths and bounded amounts before float conversion."""
    for idx, value in series.items():
        cleaned = str(value).strip().lstrip('$€£¥').replace(',', '')
        try:
            number = Decimal(cleaned)
            if not number.is_finite() or abs(number) > Decimal('1000000000000') or number != number.quantize(Decimal('0.01')):
                raise ValueError
        except Exception as exc:
            raise ValueError(f"Amount column {column!r} must contain finite values within +/- 1e12 and at most two decimals; row {idx}: {value!r}.") from exc


def money(value, currency):
    return "N/A" if value is None else f"{CURRENCIES[currency]}{value:,.2f}"


def _sum(series):
    values = series.dropna()
    result = sum((Decimal(str(value)) for value in values), Decimal(0)) if not values.empty else None
    if result is None:
        return None
    if not math.isfinite(float(result)):
        raise ValueError("Aggregated amount is not finite.")
    return round(float(result), 2)


def _rate(numerator, denominator):
    return round(numerator / denominator * 100, 2) if numerator is not None and denominator > 0 else None


def _cap(records, key, max_rows, amount_key, total, chinese):
    if len(records) <= max_rows:
        return records
    head, tail = records[:max_rows - 1], records[max_rows - 1:]
    result = {key: f"其他汇总（{len(tail)} 项）" if chinese else f"Other ({len(tail)} segments)"}
    for field in ("txns", "gross", "net", "refunded_txns", "latest_txns", "latest_refunded_txns"):
        values = [row[field] for row in tail]
        result[field] = None if any(value is None for value in values) else round(sum(values), 2)
    result["refund_pct"] = _rate(result["refunded_txns"], result["txns"])
    result["latest_refund_pct"] = _rate(result["latest_refunded_txns"], result["latest_txns"])
    result["share_pct"] = round(result[amount_key] / total * 100, 1) if total > 0 else None
    result["is_anomaly"] = None if any(row["is_anomaly"] is None for row in tail) else any(row["is_anomaly"] for row in tail)
    result["status"] = "Aggregated segments; see anomaly details"
    return head + [result]


def compute_report_metrics(df, refund_threshold=3.5, max_table_rows=6, language="en"):
    if df.empty or max_table_rows < 1:
        raise ValueError("Input must be nonempty and max-table-rows must be positive.")
    if not math.isfinite(refund_threshold) or not 0 <= refund_threshold <= 100:
        raise ValueError("Refund threshold must be between 0 and 100.")
    zh = language == "zh"
    period, channel, region, gross, fee, refund, net = (
        ("结算周期", "业务渠道", "销售大区", "总流水_USD", "通道手续费_USD", "是否退款", "净营收_USD") if zh
        else ("week", "channel", "region", "gross_usd", "stripe_fee_usd", "refunded", "net_usd")
    )
    basis = df.attrs.get("report_basis", "net")
    currency = df.attrs.get("currency", "USD")
    status = df.attrs.get("field_status", {"fee": "provided", "refund": "provided", "net": "provided"})
    amount_col = gross if basis == "gross" else net
    amount_key = basis
    total = _sum(df[amount_col])
    if total is None:
        raise ValueError("No observed amount is available for the selected report basis.")
    refund_known = df[refund].notna().all()
    audit_available = refund_known and status["refund"] == "provided"
    weekly = []
    for label, group in df.groupby(period, sort=False):
        weekly.append({period: str(label), "period": str(label), "_sort": group["_sort_key"].iloc[0], "txns": len(group), "gross": _sum(group[gross]), "net": _sum(group[net]), "refund_pct": round(float(group[refund].mean() * 100), 2) if refund_known else None})
    weekly.sort(key=lambda row: row["_sort"])
    for row in weekly:
        del row["_sort"]
    latest = weekly[-1]
    delta = None
    if len(weekly) > 1 and weekly[-2][amount_key] > 0:
        delta = round((latest[amount_key] - weekly[-2][amount_key]) / weekly[-2][amount_key] * 100, 1)
    delta_str = f"{delta:+.1f}% ({latest['period']} vs {weekly[-2]['period']})" if delta is not None else f"N/A ({latest['period']})"
    anomalies = []

    def breakdown(key):
        records = []
        for label, group in df.groupby(key):
            current = group[group[period] == latest["period"]]
            refunded = int(group[refund].sum()) if refund_known else None
            latest_refunded = int(current[refund].sum()) if refund_known else None
            cumulative_rate, latest_rate = _rate(refunded, len(group)), _rate(latest_refunded, len(current))
            anomaly = (cumulative_rate > refund_threshold or (latest_rate is not None and latest_rate > refund_threshold * 1.5)) if audit_available else None
            record = {key: str(label), "txns": len(group), "gross": _sum(group[gross]), "net": _sum(group[net]), "refunded_txns": refunded, "latest_txns": len(current), "latest_refunded_txns": latest_refunded, "refund_pct": cumulative_rate, "latest_refund_pct": latest_rate, "is_anomaly": anomaly, "status": "Not assessed" if anomaly is None else "Flagged" if anomaly else "Within threshold"}
            record["share_pct"] = round(record[amount_key] / total * 100, 1) if total > 0 else None
            records.append(record)
            if key == channel and anomaly:
                anomalies.append({channel: str(label), "cumulative_refund_pct": cumulative_rate, "latest_period_refund_pct": latest_rate, "period": latest["period"], "threshold_pct": refund_threshold})
        records.sort(key=lambda row: row[amount_key], reverse=True)
        return records

    channels, regions = breakdown(channel), breakdown(region)
    top, geography = channels[0], regions[0]
    basis_label = ("销售额" if basis == "gross" else "净额") if zh else ("gross sales" if basis == "gross" else "net amount")
    change = (f"最新一期较上期变动 {delta:+.1f}%" if delta is not None else "环比不适用或无法计算") if zh else (f"latest period changed {delta:+.1f}%" if delta is not None else "period-over-period delta not calculable (no positive prior-period amount)")
    if zh:
        first = f"主要分类“{top[channel]}”：{basis_label} {money(top[amount_key], currency)}，共 {top['txns']:,} 条记录；{change}。"
        second = "退款数据未提供，无法评估退款率或达标情况。" if not refund_known else "退款标记采用人为假设，不能据此得出退款达标结论。" if not audit_available else f"有 {len(anomalies)} 个分类触发退款预警；需结合原始记录核查。" if anomalies else f"已提供记录的退款率未触发设定阈值（累计 {refund_threshold}%，当期 {refund_threshold * 1.5:g}%）。"
        third = f"地区分布：{geography[region]} 的{basis_label}为 {money(geography[amount_key], currency)}；占比 {geography['share_pct']}%。" if geography['share_pct'] is not None else f"地区分布：{geography[region]} 的{basis_label}为 {money(geography[amount_key], currency)}；总额非正，占比不适用。"
    else:
        first = f"Largest segment — {top[channel]}: {money(top[amount_key], currency)} in {basis_label} across {top['txns']:,} records; {change}."
        second = "Refund data was not provided; refund rates and compliance cannot be assessed." if not refund_known else "Refund flags were assumed, so they do not establish observed refund compliance." if not audit_available else f"{len(anomalies)} segment(s) triggered refund thresholds; review the source records." if anomalies else f"Observed refund rates did not trigger the configured thresholds ({refund_threshold}% cumulative; {refund_threshold * 1.5:g}% latest period)."
        third = f"Geographic distribution: {geography[region]} contributed {money(geography[amount_key], currency)} ({geography['share_pct']}%) of {basis_label}." if geography['share_pct'] is not None else f"Geographic distribution: {geography[region]} contributed {money(geography[amount_key], currency)}; shares are unavailable because the total is non-positive."
    gross_total, net_total = _sum(df[gross]), _sum(df[net])
    realization = round(net_total / gross_total * 100, 1) if net_total is not None and gross_total > 0 else None
    return {"currency": currency, "report_basis": basis, "language": language, "field_status": status, "report_note": df.attrs.get("report_note", ""), "channel_key": channel, "region_key": region, "total_rows": len(df), "gross_total": gross_total, "net_total": net_total, "fee_total": _sum(df[fee]), "refund_rate": round(float(df[refund].mean() * 100), 2) if refund_known else None, "net_realization_rate_pct": realization, "aov": round(gross_total / len(df), 2), "latest_week": latest["period"], "latest_week_net": latest["net"], "period_delta_pct": delta, "period_delta_str": delta_str, "wow_net_delta": delta if basis == "net" else None, "wow_delta_str": delta_str if basis == "net" else "N/A (gross basis)", "refund_threshold": refund_threshold, "executive_bullets": [first, second, third], "anomalies": anomalies, "weekly": weekly, "by_channel": _cap(channels, channel, max_table_rows, amount_key, total, zh), "by_region": _cap(regions, region, max_table_rows, amount_key, total, zh), "total_distinct_channels": len(channels), "total_distinct_regions": len(regions)}


def render_report_charts(metrics, chart_path):
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    zh = metrics["language"] == "zh"
    basis, currency = metrics["report_basis"], metrics["currency"]
    channel = metrics["channel_key"]
    amount_name = ("销售额" if basis == "gross" else "净额") if zh else ("Gross sales" if basis == "gross" else "Net amount")
    weekly, segments = metrics["weekly"], metrics["by_channel"]
    labels = [textwrap.fill(row[channel], width=22, break_long_words=True) for row in segments]
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.5, max(2.7, len(segments) * .48)), dpi=200, layout="constrained")
    left.plot([row["period"] for row in weekly], [row[basis] / 1000 for row in weekly], color="#0F2537", marker="o", lw=2)
    left.set_title(f"{amount_name} — {currency} {'千' if zh else 'thousands'}", fontsize=10)
    left.tick_params(axis="x", rotation=30, labelsize=8)
    right.barh(range(len(segments)), [row[basis] / 1000 for row in segments], color="#D95D39", height=.5)
    right.set_yticks(range(len(segments)))
    right.set_yticklabels(labels, fontsize=8)
    right.invert_yaxis()
    right.set_title(f"{'分类贡献' if zh else 'By segment'} — {currency} {'千' if zh else 'thousands'}", fontsize=10)
    for axis in (left, right):
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y" if axis is left else "x", alpha=.2)
        axis.tick_params(labelsize=8)
    Path(chart_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(chart_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def build_report_html(metrics, chart_path, html_path):
    zh = metrics["language"] == "zh"
    currency, basis = metrics["currency"], metrics["report_basis"]
    amount_name = ("销售额" if basis == "gross" else "净额") if zh else ("Gross sales" if basis == "gross" else "Net amount")
    missing = "未提供" if zh else "Not provided"
    def esc(value):
        return html.escape(str(value))
    def financial(field, value, percent=False):
        if value is None:
            return missing
        text = f"{value}%" if percent else money(value, currency)
        state = metrics["field_status"][field]
        suffix = {"estimated": "估算" if zh else "estimated", "assumed": "假设" if zh else "assumed", "derived": "推算" if zh else "derived"}.get(state)
        return esc(text + (f" ({suffix})" if suffix else ""))
    def table(records, key, title):
        rows = []
        for row in records:
            share = f"{row['share_pct']}%" if row['share_pct'] is not None else 'N/A'
            rows.append(f"<tr><td>{esc(row[key])}</td><td>{row['txns']:,}</td><td>{esc(money(row[basis], currency))}</td><td>{share}</td></tr>")
        return f"<h2>{title}</h2><table><thead><tr><th>{'名称' if zh else 'Name'}</th><th>{'记录数' if zh else 'Records'}</th><th>{amount_name} ({currency})</th><th>{'占比' if zh else 'Share'}</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    title = "销售经营简报" if zh and basis == "gross" else "净额经营简报" if zh else "Sales briefing" if basis == "gross" else "Net revenue briefing"
    definition = ("销售额口径：未扣除退款、支付手续费、经营成本或税费；未知项不按零计算。" if zh else "Gross sales basis: before refunds, payment fees, operating costs and taxes. Unknown values are not zero.") if basis == "gross" else ("净额采用输入值或显式推算口径。支付净额留存率为净额除以销售额，不代表扣除经营成本和税费后的利润率。" if zh else "Net amounts use the supplied or explicitly derived accounting basis. Net realization is net / gross, not profit after operating costs and taxes.")
    labels = ["记录数", f"{amount_name} ({currency})", "平均每条销售额", "最新一期变化"] if zh else ["Records", f"{amount_name} ({currency})", "Avg gross / record", "Latest period change"]
    total = metrics["gross_total"] if basis == "gross" else metrics["net_total"]
    values = [f"{metrics['total_rows']:,}", money(total, currency), money(metrics['aov'], currency), metrics['period_delta_str']]
    finance = f"{'手续费' if zh else 'Fees'}: {financial('fee', metrics['fee_total'])} · {'净额' if zh else 'Net amount'}: {financial('net', metrics['net_total'])} · {'退款率' if zh else 'Refund rate'}: {financial('refund', metrics['refund_rate'], True)}"
    realization = metrics['net_realization_rate_pct']
    finance += f" · {'净额留存率' if zh else 'Net realization'}: {realization if realization is not None else missing}{'%' if realization is not None else ''}"
    chart = base64.b64encode(Path(chart_path).read_bytes()).decode('ascii')
    document = f'''<!doctype html><html lang="{'zh' if zh else 'en'}"><head><meta charset="utf-8"><style>
@page {{ size:A4; margin:12mm 14mm; }}
* {{box-sizing:border-box}} body {{font-family:'Microsoft YaHei','Noto Sans CJK SC','Arial',sans-serif;color:#182d3c;font-size:9.5pt;line-height:1.4;margin:0}}
header {{background:#0f2537;color:white;padding:10px 14px}} h1 {{font-size:17pt;margin:0 0 3px}} header p {{margin:0;font-size:9pt}}
h2 {{font-size:11pt;margin:10px 0 5px;break-after:avoid}} p {{margin:6px 0}} .note {{font-size:8.3pt;color:#475569}} ul {{padding-left:18px;margin:6px 0}} li {{margin-bottom:4px}}
table {{border-collapse:collapse;width:100%;font-size:9pt;margin-bottom:7px}} th,td {{text-align:right;padding:5px 6px;border-bottom:1px solid #dde3e8;overflow-wrap:anywhere}} th {{border-bottom:2px solid #0f2537}} th:first-child,td:first-child {{text-align:left}} tr,figure {{break-inside:avoid;page-break-inside:avoid}} figure {{margin:7px 0}} img {{width:100%;height:auto;display:block}} footer {{font-size:8pt;color:#64748b;margin-top:8px}}
</style></head><body><header><h1>{title}</h1><p>{esc(metrics['latest_week'])} | {currency} | {'记录汇总' if zh else 'Transaction-record summary'}</p></header>
<p class="note">{definition}</p><p class="note">{esc(metrics['report_note'])}</p>
<table><thead><tr>{''.join('<th>'+esc(x)+'</th>' for x in labels)}</tr></thead><tbody><tr>{''.join('<td>'+esc(x)+'</td>' for x in values)}</tr></tbody></table>
<p class="note">{finance}</p><h2>{'数据观察' if zh else 'Observed results'}</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in metrics['executive_bullets'])}</ul>
<figure><img src="data:image/png;base64,{chart}" alt="{amount_name} trend and segment contributions"></figure>
{table(metrics['by_channel'], metrics['channel_key'], '分类分布' if zh else 'Segment breakdown')}
{table(metrics['by_region'], metrics['region_key'], '地区分布' if zh else 'Geographic breakdown')}
<footer>{'币种参数只标注输入币种，不执行汇率换算。分类名称采用输入数据。' if zh else 'Currency labels describe the input; no exchange-rate conversion is performed. Segment names come from the input.'}</footer>
</body></html>'''
    Path(html_path).parent.mkdir(parents=True, exist_ok=True)
    Path(html_path).write_text(document, encoding='utf-8')
