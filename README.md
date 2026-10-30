# Deliverable-First AI Skill: Raw CSV to Board-Ready Executive PDF

Stop pasting raw CSV exports into LLM chat windows and praying the math and table formatting hold up.

This repository contains the deterministic **Codex / Claude Code Skill (`SKILL.md`)**, the **29-Word Anti-AI Executive Style Guard (`anti_ai_style_guard.md`)**, and the **Python rendering pipeline (`csv_to_pdf_pipeline.py`)** to turn 4,000+ rows of Stripe, Shopify, or SaaS operational logs into a 1-page, board-ready A4 PDF briefing in 14 seconds.

![Before and After Comparison](post_01_before_after_demo_card.png)

## Why Deterministic Pipelines Beat Prompt-Only Reporting

1. **Zero LLM Arithmetic (`pandas`):** The script computes 100% of revenue totals, week-over-week deltas, channel shares, and automated refund-spike anomaly flags before the model ever sees the data.
2. **High-DPI Vector Plots (`matplotlib`):** Pre-renders 200 DPI two-tone charts (`#0F2537` Deep Navy & `#D95D39` Terracotta) with native **English, Chinese, and Japanese (CJK)** typography support out of the box.
3. **29-Word Anti-AI Style Guard:** Strips out robotic corporate filler (*"delve"*, *"tapestry"*, *"in today's fast-paced world"*) and forces metric-led causal commentary.
4. **Zero Sliced Tables (`Headless Chromium`):** Enforces CSS `break-inside: avoid` across every table row and figure container so tables never split awkwardly across page boundaries.

## Repository Contents

| File | Description |
| :--- | :--- |
| `SKILL.md` | Drop-in agent skill definition for **Codex** and **Claude Code** |
| `anti_ai_style_guard.md` | The 29-word core constraint + hard-banned AI vocabulary linter list |
| `csv_to_pdf_pipeline.py` | English edition: generates mock 4,200-row CSV, computes KPIs, renders charts & HTML |
| `csv_to_pdf_pipeline_zh.py` | Chinese/CJK edition: native `Noto Sans CJK` rendering for Asian markets |
| `sample_executive_briefing_report.pdf` | Compiled 1-page A4 Executive Briefing sample (English) |
| `sample_executive_briefing_report_zh.pdf` | Compiled 1-page A4 Executive Briefing sample (Chinese) |

## Quick Start (Run Locally in 60 Seconds)

```bash
# 1. Install lightweight dependencies
pip install pandas numpy matplotlib pillow pypdfium2 selenium

# 2. Run the English pipeline (generates 4,200 mock rows + HTML/Charts)
python3 csv_to_pdf_pipeline.py

# 3. Or run the Chinese/CJK pipeline
python3 csv_to_pdf_pipeline_zh.py
```

## Adapting to Your Own CSV Schema

Open `csv_to_pdf_pipeline.py` and update the column mapping inside `compute_deterministic_metrics(df)`:
- Replace `gross_usd`, `net_usd`, `stripe_fee_usd`, and `refunded` with your custom export column headers.
- Adjust the anomaly threshold rule (`r["refund_pct"] > 3.5`) to match your team's SLA targets.

## License
MIT License — free for personal and commercial use.
