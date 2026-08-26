# Website QA & Technical Audit — Universal

This folder contains a standalone Python website QA and technical audit script (web_audit.py) and instructions for running it.

Overview

The audit crawls a site starting from a URL and checks:
- Link integrity (internal & external links, redirect chains, staging/dev links)
- UTM parameter validation and consistency
- Technical SEO signals (title, meta description, canonical, H1s, sitemap/robots)
- Accessibility-oriented static checks (image alt text, form labels, heading hierarchy, lang attribute)
- Site hygiene (viewport meta, favicon, duplicate analytics tags, 404 handling)

It only reads publicly reachable HTML (no login, no JS rendering, no visual contrast checks).

Files

- web_audit.py — the complete, tested Python script. Save/run it verbatim.

Quick requirements

Install the Python dependencies if you don't have them:

    python3 -c "import requests, bs4" 2>/dev/null || pip install requests beautifulsoup4

How to run

Basic run (writes reports to the current directory):

    python3 web_audit.py https://example.com

Recommended run with options:

    python3 web_audit.py https://example.com --max-pages 50 --out-dir ./reports --prefix example_

Useful flags

- --max-pages N         : crawl limit (default 50)
- --max-external-checks N: cap on external link checks (default 150)
- --wcag AAA            : stricter accessibility checks (default AA)
- --utm-config rules.json: path to a UTM convention JSON file (see below)
- --timeout N           : per-request timeout seconds (default 15)
- --prefix foo_         : filename prefix for output files
- --out-dir DIR         : directory for generated reports (default: current dir)
- --skip-seo            : exclude SEO-dimension findings (useful for internal or deindexed sites)

Outputs

The script creates four files (timestamped):
- audit_summary_<ts>.txt  — short text summary with counts and top findings
- audit_findings_<ts>.csv — every finding as a CSV table
- audit_data_<ts>.json    — raw structured JSON data
- audit_report_<ts>.html  — full color-coded HTML report

Notes & best practices

- If your environment limits long-running processes, reduce --max-pages and --max-external-checks so the crawl finishes within the allowed time.
- The script probes whether http:// destinations support https:// before suggesting switching links to https.
- It parses sitemap.xml (one level of sitemap-index recursion) so orphan pages listed in the sitemap are audited.
- Visual checks (contrast, keyboard navigation) require a browser-driven approach; this script flags those items as needing manual review.

Example minimal utm_rules.json

Save a JSON file and pass it with --utm-config if you have a UTM naming convention:

{
  "casing": "lower",
  "allowed": {
    "utm_source": ["newsletter", "twitter", "google"],
    "utm_medium": ["email", "social", "cpc"],
    "utm_campaign": null
  }
}

Re-run for fresh checks

Site content changes frequently; re-run the same command to get an updated audit. Each run writes timestamped output files so previous results are preserved.
