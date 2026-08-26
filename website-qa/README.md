# Website QA Audit

A crawler that checks a live website for broken links, tracking errors, SEO gaps, and accessibility issues, then writes a structured report. Every finding is deterministic: a link either resolves or it doesn't, a tag either exists or it doesn't. No visual review, no opinions, just what's measurably wrong and where.

## What it checks

**Link integrity** — internal and external links (status codes, redirect chains), staging or dev URLs left in by mistake, malformed `mailto:`/`tel:` links, real mixed content vs. harmless non-HTTPS outbound links.

**UTM parameters** — typos in tracking tags, inconsistent capitalization that splits your analytics into duplicate rows, links missing part of their attribution.

**Technical SEO** — duplicate titles and meta descriptions, missing or broken heading structure, missing Open Graph tags, invalid JSON-LD, sitemap and robots.txt issues, orphan pages (live and indexable but linked from nowhere).

**Accessibility** — missing, generic, or filename-as-alt image text, unlabeled form fields, generic link text ("click here"), skipped heading levels, missing `lang` attribute.

**Site hygiene** — mobile viewport tag, favicon, duplicate analytics installs, a real 404 page, leftover placeholder text.

Every finding is also tagged with a **dimension** (SEO, Accessibility, UX, Security, or Analytics) separate from its category, so results can be filtered by who a finding actually affects rather than just which audit section produced it. A missing H1, for example, is filed under SEO but tagged as an Accessibility issue, since screen reader users depend on heading structure whether or not the page is indexed.

## What it won't catch

It can't get past a login wall, measure real page speed or Core Web Vitals, judge color contrast, or evaluate keyboard navigation. Those need an actual browser. It flags them as manual-review items instead of guessing.

## Requirements

- Python 3.7+
- `requests`
- `beautifulsoup4`

```bash
pip install requests beautifulsoup4
```

## Usage

```bash
python web_audit.py https://example.com
```

Runs a full crawl from the given URL and writes four report files to the current directory: a text summary, a CSV of every finding, an HTML report, and the raw JSON.

### Options

| Flag | Default | Description |
|---|---|---|
| `--max-pages N` | 50 | Maximum pages to crawl |
| `--max-external-checks N` | 150 | Cap on external links that get a status check |
| `--wcag AA\|AAA` | AA | Accessibility strictness level |
| `--utm-config FILE` | none | JSON file describing your UTM naming convention |
| `--timeout N` | 15 | Per-request timeout in seconds |
| `--out-dir DIR` | `.` | Where to write report files |
| `--prefix STR` | none | Filename prefix, useful when auditing multiple sites |
| `--skip-seo` | off | Drop SEO-dimension findings from the report; keeps Accessibility, UX, Security, and Analytics findings. Use this for sites where search ranking doesn't matter (internal tools, unlaunched sites, deliberately deindexed pages) |

### Example

```bash
python web_audit.py https://example.com --max-pages 100 --wcag AAA --out-dir ./reports --prefix example_
```

### UTM convention file

Optional, passed via `--utm-config`:

```json
{
  "casing": "lower",
  "allowed": {
    "utm_source": ["newsletter", "twitter", "google"],
    "utm_medium": ["email", "social", "cpc"],
    "utm_campaign": null
  }
}
```

Without a config file, the script still flags internal inconsistencies (`Newsletter` vs. `newsletter`) rather than inventing a standard.

## Output files

Each run produces four timestamped files, so nothing overwrites a previous audit:

- `*_audit_summary_<timestamp>.txt` — counts and top findings
- `*_audit_findings_<timestamp>.csv` — every finding, one row each
- `*_audit_report_<timestamp>.html` — full report grouped by category, color-coded by severity
- `*_audit_data_<timestamp>.json` — raw structured data

## Using this with an AI agent

`website-qa-audit-universal.txt` in this repo bundles the full task specification (what to check, how to talk to the user, how to report results) together with the script. Hand it to any AI agent or coding assistant and ask it to adapt the workflow into its own skill, tool, or custom-instruction format. The script itself needs no adaptation; it's a fixed, tested implementation.

If you use Claude with Cowork, `website-qa-audit.skill` installs directly: download it and drop it into Cowork.

## Severity levels

- **Critical** — broken for visitors or search engines right now (404s, missing alt on informative images, duplicate titles)
- **Should-fix** — degrades the experience or the data but isn't broken (missing meta descriptions, inconsistent UTM casing)
- **Minor** — worth knowing, low urgency (short title tags, missing favicon)

## License

MIT
