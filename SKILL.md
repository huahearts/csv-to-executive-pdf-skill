---
name: csv-to-executive-pdf
description: Turns a raw business transaction CSV export into a 1-page A4 PDF briefing with strict input validation, deterministic pandas math, high-DPI matplotlib charts, and programmatic page-budget verification.
---

# CSV-to-Executive-PDF Deliverable Skill (v2.2)

When invoked on a `.csv` file, follow this strict 4-stage deterministic pipeline so the workflow never silently coerces bad data, guesses accounting rules, or produces multi-page table spills.

## Stage 1: Strict Validation & Deterministic Math (`pandas`)
1. **Strict Input & Schema Validation:**
   - Never silently generate mock data when `--input` points to a missing file; fail immediately with `FileNotFoundError` (exit code `2`). Synthetic demo data is only generated when `--demo` is explicitly passed.
   - Parse currency and numeric fields strictly (`parse_strict_numeric_series`). If any row contains unparseable strings or `NaN`, raise `ValueError` citing the exact row index rather than coercing to `0`.
   - Parse refund flags via explicit boolean mapping (`0/1`, `true/false`, `yes/no`, `y/n`, `refunded/paid`, `是/否`, `已退款/未退款`); reject unknown labels.
   - Require explicit fee and refund columns by default. Only estimate fees (`2.9% + $0.30`) or assume zero refunds when `--estimate-missing-fees` or `--assume-zero-refunds` is explicitly passed.
2. **Chronological Period Sorting & Consistent PoP Deltas:**
   - Parse period strings (`YYYY-Www` including single-digit `2026-W9`, `YYYY-MM`, `YYYY-Qq`, `YYYY-MM-DD`) into chronological sort tuples so `2026-W10` always sorts after `2026-W9`.
   - Compute Total Transactions, Gross Volume, Payment Fees, Net Revenue, Net Realization Rate (`net_realization_rate_pct`), Average Order Value (AOV), Channel/Region shares, and Refund SLA breaches.
   - When the dataset has only 1 period or prior-period net revenue is `<= 0`, set `wow_net_delta = null` and output `"N/A"` across JSON, KPI table, and executive prose (never output a contradictory `+0.0%`).
3. Export verified metrics to `summary_metrics.json`.

For sales-only datasets, use `--report-basis gross` and set the observed input currency with `--currency` (USD, GBP, EUR, CNY or JPY). Absent fee, refund and net columns must remain unavailable; never insert placeholder zeros. JSON uses `null` and the PDF states “Not provided”. Do not claim refund compliance when refund flags are missing or assumed. Gross reports show gross-sales period changes and shares, not net results. Supplied columns retain strict validation; net mode retains its required financial inputs. The fee estimator is USD-only. Currency selection does not perform conversion. Record source, period, exclusions and synthetic classifications with `--report-note`; do not present dataset classifications as observed acquisition channels. Keep `report_support.py` beside both entry-point scripts.

## Stage 2: Locked Two-Tone Vector Chart Generation (`matplotlib`)
1. Pre-render charts at 200 DPI using `matplotlib` with `bbox_inches='tight'`.
2. Enforce the executive color palette (`#0F2537` Primary Navy, `#D95D39` Accent Terracotta, `#2E6B9E` Secondary Blue) and cross-platform CJK font fallback (`Noto Sans CJK SC`, `PingFang SC`, `Microsoft YaHei`).

## Stage 3: Evidence-Bounded Commentary & Regex Vocabulary Linter
1. Construct the 3 executive summary bullets strictly from verified fields in `summary_metrics.json`. When a channel breaches `--refund-threshold`, report the exact period and cumulative refund percentages and recommend cross-checking support tickets and gateway logs—never fabricate root causes not present in the CSV.
2. Run `lint_executive_copy()` to regex-scan generated bullets against the banned AI vocabulary list in `anti_ai_style_guard.md` (`delve`, `tapestry`, `seamless`, `game-changer`, etc.). Note: this regex check enforces banned-vocabulary rules; full stylistic compliance requires following the structural patterns in `anti_ai_style_guard.md`.

## Stage 4: Headless Chromium PDF Compilation & Page-Budget Check
1. Cap breakdown tables to `--max-table-rows` (default `6`), aggregating any tail segments into `"Other (N segments)"` so high-cardinality datasets do not overflow the page.
2. Compile HTML to vector PDF via Headless Chromium (`Page.printToPDF`), and verify via `pypdfium2` that the final page count is `<= --max-pages` (default `1`). If compilation fails or exceeds the page budget, raise `RuntimeError` and exit with code `2` (unless `--allow-html-only` is passed).

Net realization rate is net revenue divided by gross volume, typically gross less fees and refunds. It excludes operating overhead, COGS and taxes; provided net values follow the source accounting definition.
