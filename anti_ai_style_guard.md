# The 29-Word Anti-AI Executive Style Guard (`anti_ai_style_guard.md`)

Paste the 29-word core constraint below into your system prompt, `CLAUDE.md`, or Codex `SKILL.md` whenever generating client-facing reports, executive memos, or X posts.

## 1. The 29-Word Core Constraint
> **"Write with ruthless engineering precision. State exact numbers, causes, and trade-offs in active voice. Cut every introductory throat-clearing sentence, corporate buzzword, and generic conclusion. Let verified data speak."**

## 2. Hard-Banned Vocabulary List (Automatic Linter Reject)
Never allow the model to output any of the following words or phrases:
- **Banned Verbs:** `delve`, `embark`, `unleash`, `unlock`, `elevate`, `revolutionize`, `foster`, `harness`, `spearhead`, `supercharge`, `skyrocket`, `navigate`
- **Banned Nouns & Adjectives:** `tapestry`, `testament`, `landscape`, `paradigm`, `synergy`, `game-changer`, `cutting-edge`, `seamless`, `robust`, `comprehensive`, `ever-evolving`, `fast-paced`, `holistic`, `pivotal`, `crucial`
- **Banned Openers & Closers:**
  - *"In today's fast-paced digital world..."*
  - *"It's important to remember that..."*
  - *"Not only X, but also Y..."*
  - *"In conclusion, by leveraging..."*
  - *"Here's the kicker:"* / *"Let's dive right in!"*

## 3. Structural Replacement Rules for X Posts & Executive Briefings
| Amateur AI Slop (Delete) | Deterministic Engineer Style (Use Instead) |
| :--- | :--- |
| *"This workflow is blazingly fast and saves tons of time."* | *"Execution time dropped from 2.5 hours to 14 seconds across 4,200 rows."* |
| *"AI helps you seamlessly format your messy spreadsheets."* | *"`openpyxl` locks column widths and injects native `=SUM()` formulas with zero `#REF!` errors."* |
| *"Here are 5 amazing tips to supercharge your productivity:"* | *"3 geometric guardrails that stop `python-pptx` from overflowing slide margins:"* |
