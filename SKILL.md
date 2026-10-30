---
name: csv-to-executive-pdf
description: Turns any raw business CSV export (Stripe, Shopify, SaaS metrics, or ops logs) into a board-ready A4 PDF briefing with deterministic pandas math, high-DPI matplotlib charts, and zero page-break table slicing.
---

# CSV-to-Executive-PDF Deliverable Skill

When invoked on a raw `.csv` file, follow this strict 4-stage deterministic pipeline so the LLM never hallucinates numbers or produces broken page layouts.

## Stage 1: Deterministic Math & Anomaly Extraction (Zero LLM Arithmetic)
1. Never calculate sums, averages, growth rates, or percentages inside the LLM prompt.
2. Run `csv_to_pdf_pipeline.py` (using `pandas`) to compute:
   - Total Revenue, Total Orders/Transactions, Average Order Value (AOV), and Net Margin.
   - Period-over-Period (PoP) or Week-over-Week (WoW) delta percentages.
   - Top 3 performing categories/SKUs and Top 3 anomaly flags (e.g., refund spikes or margin drops > 15%).
3. Export these verified metrics into `summary_metrics.json`.

## Stage 2: Locked Two-Tone Vector Chart Generation
1. Pre-render charts at 200 DPI using `matplotlib` with `bbox_inches='tight'`.
2. Enforce the executive color palette:
   - Primary Navy: `#0F2537`
   - Accent Terracotta: `#D95D39`
   - Muted Slate: `#64748B`
3. Remove top and right axes spines (`ax.spines['top'].set_visible(False)`) and never use default rainbow palettes.

## Stage 3: Anti-AI Executive Commentary (Strict Negative Constraints)
When drafting the 3-bullet executive summary from `summary_metrics.json`, obey `anti_ai_style_guard.md`:
- Lead every bullet with a concrete metric and causal driver (e.g., *"Enterprise annual plans drove 64.2% of net revenue ($48,210), offsetting a 12.4% dip in self-serve checkouts"*).
- Strictly ban filler phrases (`delve`, `tapestry`, `testament`, `in today's fast-paced world`, `game-changer`, `unlocking`).

## Stage 4: Headless Chromium Vector PDF Compilation & Page-Break Guardrails
1. Inject the verified metrics table, 3-bullet summary, and high-DPI chart into the A4 HTML/CSS template.
2. Enforce `break-inside: avoid; page-break-inside: avoid;` on all `tr` and `figure` elements so tables and charts are never sliced across pages.
3. Compile via headless Chromium (`Page.printToPDF`) to produce the final client-ready PDF.
