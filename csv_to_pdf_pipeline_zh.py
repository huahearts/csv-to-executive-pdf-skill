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

from report_support import CURRENCIES, compute_report_metrics, render_report_charts, build_report_html

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

    basis = getattr(args, "report_basis", "net")
    currency = getattr(args, "currency", "USD")
    if args.estimate_missing_fees and currency != "USD":
        raise ValueError("The built-in fee estimate is USD-only; provide actual fees for other currencies.")
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
    elif basis == "gross":
        df["通道手续费_USD"] = float("nan")
    else:
        raise ValueError(
            f"CSV 缺少手续费列 '{args.col_fee}'。请通过 '--col-fee' 指定列名，或显式传入 '--estimate-missing-fees' 启用估算。"
        )

    if args.col_refund in raw_df.columns:
        df["是否退款"] = parse_strict_refund_series(raw_df[args.col_refund], args.col_refund)
    elif args.assume_zero_refunds:
        print(f"[WARN] 缺少退款列 '{args.col_refund}'，已按 --assume-zero-refunds 显式授权默认未退款。")
        df["是否退款"] = 0
    elif basis == "gross":
        df["是否退款"] = float("nan")
    else:
        raise ValueError(
            f"CSV 缺少退款标记列 '{args.col_refund}'。请通过 '--col-refund' 指定列名，或显式传入 '--assume-zero-refunds'。"
        )

    if args.col_net and args.col_net in raw_df.columns:
        df["净营收_USD"] = parse_strict_numeric_series(raw_df[args.col_net], args.col_net)
    elif args.derive_net_from_gross or not args.col_net:
        if df["通道手续费_USD"].isna().any() or df["是否退款"].isna().any():
            raise ValueError("Cannot derive net amount when fee or refund data is unavailable.")
        df["净营收_USD"] = np.where(df["是否退款"] == 1, 0.0, df["总流水_USD"] - df["通道手续费_USD"]).round(2)
    elif basis == "gross":
        df["净营收_USD"] = float("nan")
    else:
        raise ValueError(
            f"CSV 缺少净营收列 '{args.col_net}'。请传入 '--derive-net-from-gross' 允许按 (未退款时总流水 - 手续费) 推算。"
        )

    df.attrs.update(
        report_basis=basis, currency=currency, report_note=getattr(args, "report_note", ""),
        field_status={
            "fee": "provided" if args.col_fee in raw_df.columns else "estimated" if args.estimate_missing_fees else "unavailable",
            "refund": "provided" if args.col_refund in raw_df.columns else "assumed" if args.assume_zero_refunds else "unavailable",
            "net": "provided" if args.col_net in raw_df.columns else "derived" if args.derive_net_from_gross or not args.col_net else "unavailable",
        },
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
    return compute_report_metrics(df, refund_threshold, max_table_rows, language="zh")


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
    render_report_charts(metrics, chart_path)

def build_pdf_html_zh(metrics: dict, chart_path: str, html_path: str) -> None:
    build_report_html(metrics, chart_path, html_path)


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
    parser.add_argument("--currency", choices=list(CURRENCIES), default="USD", help="Input currency; labeling only, no conversion.")
    parser.add_argument("--report-basis", choices=["net", "gross"], default="net", help="Gross mode permits unavailable fee/refund/net columns; net mode remains strict.")
    parser.add_argument("--report-note", default="", help="Source, scope and exclusions shown on the report.")
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
            json.dump(metrics_data, jf, indent=2, ensure_ascii=False, allow_nan=False)
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
        print(f"[SUCCESS] 中文 PDF 管线完成，耗时 {elapsed}s。" if pdf_ok else f"[HTML ONLY] 未生成 PDF，耗时 {elapsed}s。")
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        sys.stderr.write(f"[PIPELINE ERROR] {err}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
