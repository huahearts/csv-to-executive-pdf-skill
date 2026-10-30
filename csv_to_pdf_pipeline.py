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

    basis = getattr(args, "report_basis", "net")
    currency = getattr(args, "currency", "USD")
    if args.estimate_missing_fees and currency != "USD":
        raise ValueError("The built-in fee estimate is USD-only; provide actual fees for other currencies.")
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
    elif basis == "gross":
        df["stripe_fee_usd"] = float("nan")
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
    elif basis == "gross":
        df["refunded"] = float("nan")
    else:
        raise ValueError(
            f"Refund column '{args.col_refund}' is missing from CSV. Pass '--col-refund <column_name>' or "
            f"explicitly opt into '--assume-zero-refunds'."
        )

    # Net revenue handling
    if args.col_net and args.col_net in raw_df.columns:
        df["net_usd"] = parse_strict_numeric_series(raw_df[args.col_net], args.col_net)
    elif args.derive_net_from_gross or not args.col_net:
        if df["stripe_fee_usd"].isna().any() or df["refunded"].isna().any():
            raise ValueError("Cannot derive net amount when fee or refund data is unavailable.")
        df["net_usd"] = np.where(df["refunded"] == 1, 0.0, df["gross_usd"] - df["stripe_fee_usd"]).round(2)
    elif basis == "gross":
        df["net_usd"] = float("nan")
    else:
        raise ValueError(
            f"Net revenue column '{args.col_net}' is missing from CSV. Pass '--derive-net-from-gross' "
            f"to compute net_usd = (0 if refunded else gross - fee)."
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
    return compute_report_metrics(df, refund_threshold, max_table_rows, language="en")


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
    render_report_charts(metrics, chart_path)

def build_pdf_html(metrics: dict, chart_path: str, html_path: str) -> None:
    build_report_html(metrics, chart_path, html_path)


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
    parser.add_argument("--currency", choices=list(CURRENCIES), default="USD", help="Input currency; labeling only, no conversion.")
    parser.add_argument("--report-basis", choices=["net", "gross"], default="net", help="Gross mode permits unavailable fee/refund/net columns; net mode remains strict.")
    parser.add_argument("--report-note", default="", help="Source, scope and exclusions shown on the report.")
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
            json.dump(metrics_data, jf, indent=2, ensure_ascii=False, allow_nan=False)
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
        print(f"[SUCCESS] PDF pipeline completed in {elapsed}s." if pdf_ok else f"[HTML ONLY] PDF not produced; elapsed {elapsed}s.")
    except (FileNotFoundError, ValueError, RuntimeError) as err:
        sys.stderr.write(f"[PIPELINE ERROR] {err}\n")
        sys.exit(2)


if __name__ == "__main__":
    main()
