# ABM 1:Few Journey Gem

A small, dependency-free Python script that generates a system prompt for
a **1:Few ABM (Account-Based Marketing) journey designer** — an AI
assistant that turns a cluster of ~15 target accounts, a buying group,
and your existing content toolkit into a 3–6 month campaign journey
draft.

It's model-agnostic on purpose: `abm_journey_gem.py` doesn't call any AI
API. It just renders text. Paste the output into whatever tool your team
already uses — Gemini Gems, a Claude Project, a ChatGPT Custom GPT,
Copilot Studio, or a raw system prompt via any provider's API.

This started life as a single hardcoded "Gem" prompt built for one
company's product. This version pulls every company-specific detail
(name, product areas, jargon, systems of record) into config, so any
team can generate their own version without editing prose by hand.

## Quickstart

No install required — standard library only, Python 3.8+.

```bash
python3 abm_journey_gem.py \
  --company-name "Acme Corp" \
  --solution-focus "Fraud Detection,Realtime Analytics,Data Migration" \
  --industry-jargon "SLA,API,SOC 2,uptime" \
  --with-checklist
```

That prints the full prompt (plus the human-facing data-inputs checklist)
to stdout. To save it to a file instead:

```bash
python3 abm_journey_gem.py --company-name "Acme Corp" -o prompt.txt
```

To drive it from a config file instead of flags:

```bash
python3 abm_journey_gem.py --write-sample-config config.json
# edit config.json to match your company
python3 abm_journey_gem.py --config config.json -o prompt.txt
```

CLI flags override whatever a `--config` file sets, so you can keep a
team-wide config checked into this repo and still tweak one field for a
one-off run.

## What gets generated

The script has two building blocks:

1. **The prompt** (`build_gem_prompt`) — the actual instructions the AI
   tool runs on every session: its role, the discovery questions it must
   ask before drafting anything, the research it should do, and the
   exact structure of the deliverable (journey logic, a month-by-month
   journey table, a content gap analysis, and a closing instruction).
2. **The checklist** (`build_data_inputs_checklist`, included with
   `--with-checklist`) — a short, human-facing list of what to have ready
   *before* you open a session, plus the starter prompt to kick it off.

## Configuration reference

| Field | CLI flag | Default | What it controls |
|---|---|---|---|
| `company_name` | `--company-name` | `Your Company` | Used in the assistant's name/role. |
| `gem_name` | `--gem-name` | `1:Few Journey Designer` | The rest of the assistant's name. |
| `buying_group_size` | `--buying-group-size` | `15` | Target account cluster size. |
| `buying_group_roles` | `--buying-group-roles` (comma-separated) | Technical Buyer, Economic Buyer, Executive Sponsor | Example roles in the buying group. |
| `journey_duration_min` / `journey_duration_max` | `--duration-min` / `--duration-max` | `3` / `6` | Journey length in months; also drives the stage-to-month table. |
| `solution_focus_examples` | `--solution-focus` (comma-separated) | placeholder solution areas | Your product/solution lines. |
| `industry_jargon_examples` | `--industry-jargon` (comma-separated) | API, SLA, ROI, TCO | Vocabulary for the "internal-ready, technical" tone instruction. |
| `source_system_examples` | `--source-systems` (comma-separated) | CRM, product/usage DB, data warehouse | Systems the guardrails explicitly forbid feeding in raw. |
| `bi_report_examples` | `--bi-reports` (comma-separated) | Tableau/Looker, Salesforce | What outputs should be sanity-checked against. |
| `journey_template_name` | `--journey-template-name` | `Journey Mapping Template` | Where the final draft should get filed. |
| `playbook_name` | `--playbook-name` | `your ABM Playbook` | Name of your internal playbook doc. |

## The data-privacy guardrails

These are **hardcoded in the generated prompt on every run** — they
aren't a config toggle, and the prompt itself instructs the AI tool not
to let later chat messages override them:

| DO | DON'T |
|---|---|
| Use aggregated, non-identifiable engagement data (clicks, web behavior, pipeline influence) at the account/segment level. | Accept raw customer records, logs, or exports from your systems of record — directly, via file, or pasted in chat. |
| Work with groups of accounts, not identifiable individuals. | Accept or use financial, health, HR, or legal/privileged information. |
| Sanity-check every AI-generated suggestion against your own source reports before using it. | Treat the AI tool's output as a system of record — it's a drafting aid, not ground truth. |

The one thing that *is* configurable is which systems and reports get
named in that guardrail language (`--source-systems`, `--bi-reports`) —
so the rule stays specific to your actual tech stack instead of naming a
tool you don't use.

If you're adapting this for a new team, keep these guardrails intact.
They exist so the AI tool never sees individually identifiable customer
data, financial/health/HR/legal content, or anything that should live
only in a system of record.

## Using the output with an AI tool

The generated text is a plain system prompt / custom-instructions block.
Roughly, for any AI tool:

1. Generate the prompt (`python3 abm_journey_gem.py ... -o prompt.txt`).
2. Create a new persistent assistant/project/custom-GPT/Gem in whichever
   AI tool your team uses, and paste `prompt.txt` in as its
   system prompt / instructions / custom knowledge.
3. Start a session by typing `Hello`, then provide the inputs from the
   Data Inputs Checklist (industry, buying group, content toolkit,
   proposed channels, messaging overview, and an aggregated engagement
   summary — never raw account-level exports).
4. Review the draft against the guardrails above before it goes anywhere
   near a real campaign brief.

Because the output is just text with no API calls or tool-specific
syntax, it works the same way whether the destination is a chat UI's
"custom instructions" field or the `system` parameter of an API call —
there's nothing in this repo to swap out when your team changes AI
vendors.

## Extending it

- Want a different deliverable structure? Edit the f-string in
  `build_gem_prompt()` in `abm_journey_gem.py` — it's plain text, not a
  templating engine, so it's easy to read and easy to change.
- Want more config-driven fields (e.g., a fixed list of allowed
  channels)? Add a field to `GemConfig`, wire it into the f-string, and
  add a matching CLI flag in `_build_arg_parser()`.
- Want to version different teams' configs? Keep multiple JSON config
  files in this repo (e.g., `configs/product-team-a.json`) and point
  `--config` at whichever one you need.

## Files in this repo

- `abm_journey_gem.py` — the prompt-builder script (CLI + importable
  functions, no dependencies).
- `README.md` — this file.

## Disclaimer

Output from the generated prompt is a first-pass draft aid, not a
finished campaign brief. It's meant to be reviewed by a marketer and
checked against real source data before it's used.
