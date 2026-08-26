# Segmentation Analyzer

An interactive, question-driven workflow for AI agents to help marketing
users discover behavioral segments and trends across a customer dataset
(firmographic, persona-level, and behavioral/engagement data) — for ABM
prioritization, campaign design, and journey/content-effectiveness
analysis.

This is **not** a one-shot "upload data, get a report" tool. It's a
conversation flow: the agent asks what the user actually wants to know,
figures out what data that specific question needs, checks whether the
uploaded data actually meets that bar, and only then runs real statistical
computation — never a reasoned-out guess dressed up as an analysis.

It works with any AI agent that can read this README and execute Python
(Claude, GPT-based agents, or anything similar) — there's nothing
Claude-specific here.

## What's in this repo

- **`segmentation_analyzer.py`** — the computation backend. Two
  subcommands, `sufficiency` and `cluster`, that do the actual
  statistics (pandas/scikit-learn) — the parts of the workflow that must
  never be estimated or reasoned through by the agent.
- **`README.md`** (this file) — the full workflow the agent should follow,
  plus usage docs for the script.

## Setup

```bash
pip install pandas numpy scikit-learn
```

## The workflow an agent should follow

Point your AI agent at this README (or paste its "Workflow steps" section
into its instructions) along with `segmentation_analyzer.py`. The agent
should follow these 8 steps, in order, as a conversation — not skip ahead
to running the analysis.

### Trigger conditions

Use this workflow whenever a user wants to analyze customer behavior data
for segmentation, clustering, ABM tiering, conversion path analysis, or
journey/content effectiveness — even if they don't use those exact words.
Also trigger for open-ended "what patterns exist in my data" requests.
Examples: "help me figure out which accounts deserve 1:1 treatment,"
"where are people dropping off in our nurture stream," "what's working
better, email or webinar," "just show me what's interesting in this
data."

### Step 1 — Ask the question

Don't analyze anything yet. Ask the user what they want to know about
their audience. Offer a starter menu, but let them go off-menu too:

- Where do people convert (or drop off) in a nurture stream?
- How does channel/content performance compare?
- How does engagement correlate with journey stage?
- Which accounts/personas warrant 1:1 vs. 1:few vs. 1:many treatment?
- Where's the churn or drop-off happening?
- "Just show me what's there" (open-ended discovery)

### Step 2 — Work out the data requirements for THIS question

Reason through it fresh each time — don't use a fixed lookup table:

- What's the unit of analysis? (account, persona, account-persona pair,
  individual engagement event?)
- What variables does the question actually involve?
- What kind of comparison is this? (grouping/clustering, before-after,
  correlation, funnel/time-series?)

State in plain language what data and depth would be needed for a
statistically sound answer to *this specific question*. A churn question
needs a long time window (months, not days). A channel comparison needs
enough accounts *per channel* to compare, not just a large dataset
overall. A tiering question needs enough firmographic and engagement
variables per account to distinguish tiers meaningfully. Say this to the
user before moving on — it sets up the sufficiency check in step 5.

### Step 3 — Ask for output format

Ask how they want results delivered — they can pick more than one:

- Executive summary
- Detailed report
- Segment profile cards (one per cluster)
- Raw data table (cluster assignments + driving variables, for export)
- Visual (cluster map / chart)

Also ask directly whether they want downloadable files or just the answer
in conversation. A visual should always be paired with at least one other
format, never delivered standalone.

### Step 4 — Data intake with a format check

Ask directly: is the data already structured into three layers with a
consistent linking key, or does it need cleanup first?

- **Firmographic** — account-level attributes (industry, size, revenue
  band, region, etc.)
- **Persona** — persona/contact-level attributes (role, seniority, etc.)
- **Behavioral/engagement** — events over time (channel, content touched,
  journey stage, timestamps, conversions)

All three need a consistent account/persona ID linking them.

If cleanup or column-mapping is needed: **don't guess column meanings.**
Walk through mapping each of the user's actual columns to a role (linking
key / firmographic attribute / persona attribute / behavioral metric)
with them directly, one at a time. Every org's export looks different —
ask, don't assume.

**State this plainly before any upload or analysis:** PII must be
stripped by the user before sharing data — names, emails, phone numbers,
physical addresses, and any unique personal identifiers. Persona-level and
firmographic tags (role, seniority, industry, size, etc.) should be kept
and linked to a consistent account/persona ID.

**De-identification is the user's/their organization's responsibility.**
State it as a precondition for proceeding. Don't attempt to scan, verify,
or perform de-identification yourself.

### Step 5 — Sufficiency check (real computation, not a guess)

Run `segmentation_analyzer.py sufficiency` against the requirements from
step 2 — don't eyeball the data or estimate whether it's "probably
enough":

```bash
python segmentation_analyzer.py sufficiency \
  --data accounts.csv \
  --group-col channel --min-per-group 30 \
  --feature-cols employees,engagement_score --min-samples-per-feature 15 \
  --date-col last_engagement_date --min-window-days 90
```

Exit code 0 means every requested check passed; exit code 1 means at
least one failed (see the JSON output for which). Defaults (30
rows/group, 15 samples per clustering feature, 90-day window) are
standard rules of thumb — adjust them per question using your step-2
reasoning if it clearly calls for something different, and say so.

Report the JSON result to the user honestly, with real numbers ("18
accounts in the Email channel, minimum for a stable comparison is ~30").
If any check fails, ask the user to choose:

**(a)** Wait and gather more data, or
**(b)** Proceed in directional-only mode.

If offering (b), explain the pitfalls first: clusters found on
insufficient data may not be stable, could shift significantly with more
data, and shouldn't be used alone to justify major resourcing or budget
decisions. Weight your recommendation by how far short the data falls and
what the answer will be used for — say which way you lean, but let them
decide.

### Step 6 — Run the analysis (real computation, not a guess)

Run `segmentation_analyzer.py cluster` — real K-means with a built-in
stability check (silhouette score + variance explained):

```bash
python segmentation_analyzer.py cluster \
  --data accounts.csv \
  --id-col account_id \
  --numeric-cols employees,engagement_score,content_downloads \
  --categorical-cols industry \
  --out-assignments cluster_assignments.csv \
  --out-summary cluster_summary.json
```

For questions that aren't naturally a clustering problem (e.g. a simple
funnel/drop-off analysis), write a short purpose-built pandas script
instead — real computation on the real data either way, never estimated
or reasoned-out numbers standing in for an actual result.

Read `chosen_silhouette_score` / `stability` in the output — this is what
lets you say concretely, later, whether the groupings hold up or are
directional-only. Rough guide: **0.5+** is strong separation, **0.25–0.5**
is moderate, **below 0.25** is weak — treat weak results as
directional-only everywhere they're used.

### Step 7 — Interpret and apply lenses

Translate the computed output into plain English. Name each segment in
marketing terms a person would use in a meeting ("High-Intent Enterprise
Evaluators," not "Cluster 2").

Apply whichever lens fits the original question:

- **ABM tiering lens** — which accounts/personas warrant 1:1 (high-touch,
  sales-led), 1:few (targeted campaigns to a defined group), or 1:many
  (broad automated nurture) treatment, and why.
- **Journey-effectiveness lens** — which content, channel, or messaging
  performed best at which journey stage.

### Step 8 — Deliver in the chosen format(s)

Build whichever format(s) the user picked in step 3. **Every format,
without exception, must visibly state the confidence/stability flag**
from step 6 — even a one-line executive summary needs a sentence saying
whether results are based on stable clusters or are directional-only.
Don't bury this in an appendix.

| Format | Contents |
|---|---|
| Executive summary | Direct answer to the original question, 2–3 key findings, one "so what" for decision-making, confidence flag. |
| Detailed report | Methodology, sufficiency check results, full clusters/patterns with supporting stats, ABM/journey interpretation, caveats including directional-only flag if applicable. |
| Segment profile cards | One per cluster: marketing-friendly name, who's in it, defining behaviors/firmographics, recommended ABM tier, content/channel recommendation if journey lens applies. |
| Raw data table | `cluster_assignments.csv` from step 6, as-is — don't rebuild it. |
| Visual | Cluster map/chart showing segment separation and size, always paired with another format. |

## Script reference

### `sufficiency`

| Flag | Meaning |
|---|---|
| `--data` (required) | Path to CSV. |
| `--group-col` | Column to check group sizes for. |
| `--min-per-group` | Minimum rows/group (default 30). |
| `--feature-cols` | Comma-separated clustering feature columns, to check sample-to-feature ratio. |
| `--min-samples-per-feature` | Minimum samples per feature (default 15). |
| `--date-col` | Timestamp column, for time-window coverage checks. |
| `--min-window-days` | Minimum days of history required (default 90). |
| `--missingness-cols` | Comma-separated columns to check missing-data rate for. |
| `--max-missing-pct` | Max allowed % missing per column (default 20). |
| `--out` | Optional path to also write the JSON result to. |

Exit code: `0` if all requested checks pass, `1` if any fail, `2` if no
checks were requested.

### `cluster`

| Flag | Meaning |
|---|---|
| `--data` (required) | Path to CSV. |
| `--id-col` (required) | Column identifying each row (account_id, persona_id, etc.). |
| `--numeric-cols` | Comma-separated numeric feature columns. |
| `--categorical-cols` | Comma-separated categorical feature columns. |
| `--k-min` / `--k-max` | Range of cluster counts to try (default 2–6). |
| `--out-assignments` | Output CSV of per-row cluster assignments (default `cluster_assignments.csv`). |
| `--out-summary` | Output JSON with silhouette scores, variance explained, and per-cluster profiles (default `cluster_summary.json`). |

Standardizes numeric features and one-hot encodes categorical features
before clustering, tries each k in the given range, and picks the one
with the best silhouette score.

## Compliance note

De-identification of uploaded data is the responsibility of the user and
their organization. This workflow requires it as a precondition (see Step
4) but does not attempt to verify or perform it.
