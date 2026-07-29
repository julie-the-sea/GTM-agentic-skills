#!/usr/bin/env python3
"""
web_audit.py - Website QA & technical audit crawler.

Crawls a site starting from a URL, checks link integrity, UTM parameters,
technical SEO, accessibility signals, and site hygiene, then writes four
report files (summary txt, findings csv, data json, report html).

Usage:
    python web_audit.py https://example.com
    python web_audit.py https://example.com --max-pages 100 --wcag AAA
    python web_audit.py https://example.com --utm-config utm_rules.json
    python web_audit.py https://example.com --out-dir ./reports --prefix mysite

utm_rules.json format (optional):
{
  "casing": "lower",
  "allowed": {
    "utm_source": ["newsletter", "twitter", "google"],
    "utm_medium": ["email", "social", "cpc"],
    "utm_campaign": null
  }
}
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import difflib
from collections import deque, defaultdict
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlsplit, parse_qsl

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: requests. Install with `pip install requests`.")

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: beautifulsoup4. Install with `pip install beautifulsoup4`.")

USER_AGENT = "Mozilla/5.0 (compatible; WebsiteQABot/1.0)"
TIMEOUT = 15
CANONICAL_UTM_PARAMS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]
GENERIC_ALT_PHRASES = {"image", "photo", "graphic", "picture", "img", "untitled", "placeholder"}
GENERIC_LINK_TEXT = {"click here", "here", "read more", "learn more", "more", "link", "this page", "go"}
STAGING_KEYWORDS = ["staging.", "dev.", ".local", "localhost", "test.", "sandbox.", "-staging", "-dev"]
SEVERITY_ORDER = {"critical": 0, "should-fix": 1, "minor": 2}


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def resolve_ca_bundle():
    """Some sandboxed shells route HTTPS through a local proxy that re-signs certs with a CA
    the system trust store knows about but Python's bundled certifi store does not. If that
    system bundle exists, prefer it so requests doesn't fail with spurious
    SSLError/CERTIFICATE_VERIFY_FAILED on perfectly valid sites."""
    for candidate in (
        os.environ.get("REQUESTS_CA_BUNDLE"),
        os.environ.get("SSL_CERT_FILE"),
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return True  # fall back to requests/certifi default verification


def normalize_url(url):
    """Strip fragment, trailing slash noise, keep query string."""
    parts = urlsplit(url)
    path = parts.path or "/"
    normalized = parts._replace(fragment="", path=path)
    return normalized.geturl()


def same_domain(a, b):
    da = urlparse(a).netloc.lower().lstrip("www.")
    db = urlparse(b).netloc.lower().lstrip("www.")
    return da == db


# Every finding carries a "dimension" in addition to its category/severity. Category is the
# audit section (Links, UTM, SEO, Accessibility, Site Hygiene); dimension is who the finding
# actually matters to. This lets a report be filtered after the fact. DIMENSIONS:
#   "SEO"           - matters only for search ranking/indexing
#   "Accessibility" - screen readers, keyboard nav - matters regardless of indexing
#   "UX"            - visitors hit this directly: broken links, confusing pages, social
#                     share appearance, mobile responsiveness
#   "Security"      - mixed content / insecure embedded resources
#   "Analytics"     - tracking/attribution accuracy (UTM params, duplicate analytics installs)
# Each category has a sensible default dimension (see CATEGORY_DEFAULT_DIMENSION below) so most
# call sites don't need to pass one explicitly - only pass dimension= when a finding's category
# doesn't match who it actually affects (e.g. H1 structure is filed under "SEO" category but its
# dimension is "Accessibility", since screen-reader users depend on heading hierarchy whether or
# not the site is indexed).
CATEGORY_DEFAULT_DIMENSION = {
    "SEO": "SEO",
    "Accessibility": "Accessibility",
    "UTM": "Analytics",
    "Links": "UX",
    "Site Hygiene": "UX",
}


class Finding:
    __slots__ = ["category", "severity", "dimension", "page", "location", "description", "fix"]

    def __init__(self, category, severity, page, location, description, fix, dimension=None):
        self.category = category
        self.severity = severity
        self.dimension = dimension or CATEGORY_DEFAULT_DIMENSION.get(category, "UX")
        self.page = page
        self.location = location
        self.description = description
        self.fix = fix

    def as_dict(self):
        return {
            "category": self.category,
            "severity": self.severity,
            "dimension": self.dimension,
            "page": self.page,
            "location": self.location,
            "description": self.description,
            "fix": self.fix,
        }


class Auditor:
    def __init__(self, start_url, max_pages=50, wcag="AA", utm_rules=None, timeout=TIMEOUT,
                 max_external_checks=150, skip_seo=False):
        self.start_url = normalize_url(start_url)
        self.max_pages = max_pages
        self.wcag = wcag
        self.utm_rules = utm_rules or {}
        self.timeout = timeout
        self.max_external_checks = max_external_checks
        # If the user just doesn't care about search ranking for this site, SEO-dimension
        # findings - duplicate titles, missing meta descriptions, orphan pages, sitemap/robots
        # issues - are all moot. Everything still gets *computed* (cheap, and the sitemap crawl
        # still runs so orphan pages get visited for their OTHER issues); this flag only affects
        # what makes it into the written reports.
        self.skip_seo = skip_seo

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.session.verify = resolve_ca_bundle()

        self.visited_pages = {}          # url -> page data dict
        self.link_status_cache = {}      # url -> (status, final_url, hop_count, error)
        self.https_support_cache = {}    # netloc -> bool (does an https:// version of this host respond?)
        self.findings = []
        self.utm_param_variants = defaultdict(set)   # param_name_lower -> set of raw casings seen
        self.utm_values_seen = defaultdict(set)
        self.ga_ids_seen = defaultdict(list)          # measurement id -> [pages]
        self.title_map = defaultdict(list)
        self.desc_map = defaultdict(list)
        self.internal_link_targets = set()            # pages that are linked to internally
        self.errors = []

    # ---------- HTTP helpers ----------

    def fetch(self, url):
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            return resp, None
        except requests.exceptions.RequestException as e:
            return None, str(e)

    def check_link_status(self, url):
        """Return (status_code_or_None, final_url, redirect_hops, error_str)."""
        if url in self.link_status_cache:
            return self.link_status_cache[url]
        result = self._check_link_status_uncached(url)
        self.link_status_cache[url] = result
        return result

    def probe_https_support(self, netloc):
        """Does this host respond at all over https? Cached per host. Used so we don't tell
        someone to 'just switch this link to https://' when the destination doesn't support
        HTTPS at all - that's not a fixable typo, it's a fact about someone else's server."""
        if netloc in self.https_support_cache:
            return self.https_support_cache[netloc]
        try:
            self.session.head(f"https://{netloc}/", timeout=self.timeout, allow_redirects=True)
            result = True
        except requests.exceptions.RequestException:
            result = False
        self.https_support_cache[netloc] = result
        return result

    def _check_link_status_uncached(self, url):
        try:
            resp = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if resp.status_code >= 400 or resp.status_code == 405:
                # some servers reject HEAD; fall back to GET
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            hops = len(resp.history)
            return (resp.status_code, resp.url, hops, None)
        except requests.exceptions.RequestException as e:
            return (None, url, 0, str(e))

    # ---------- crawl ----------

    def discover_sitemap_urls(self, sitemap_url=None, depth=0):
        """Parse sitemap.xml for <loc> entries and feed them into the crawl queue. Without this,
        the crawler only ever finds pages reachable by following <a href> links from the start
        URL - which means a page that's live, indexable, and listed in the sitemap but not
        linked from anywhere in the nav (an orphan) never gets visited or audited at all. Handles
        both a plain <urlset> and a <sitemapindex> that points to child sitemaps (one level of
        recursion, capped, since sitemap indexes can in principle nest arbitrarily deep)."""
        if depth > 1:
            return []
        base = f"{urlparse(self.start_url).scheme}://{urlparse(self.start_url).netloc}"
        sitemap_url = sitemap_url or urljoin(base, "/sitemap.xml")
        resp, err = self.fetch(sitemap_url)
        if err or not resp or resp.status_code >= 400:
            return []
        text = resp.text
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text)
        if "<sitemapindex" in text[:2000] and depth == 0:
            found = []
            for child in locs[:20]:  # cap: don't chase an unbounded number of child sitemaps
                found.extend(self.discover_sitemap_urls(child, depth=depth + 1))
            return found
        return locs

    def crawl(self):
        queue = deque([self.start_url])
        seen = {self.start_url}
        start_domain_netloc = urlparse(self.start_url).netloc

        for loc in self.discover_sitemap_urls():
            norm = normalize_url(loc)
            if urlparse(norm).netloc != start_domain_netloc:
                continue
            if norm not in seen and len(seen) < self.max_pages * 3:
                seen.add(norm)
                queue.append(norm)

        while queue and len(self.visited_pages) < self.max_pages:
            url = queue.popleft()
            resp, err = self.fetch(url)
            page_record = {
                "url": url,
                "status": resp.status_code if resp else None,
                "error": err,
                "final_url": resp.url if resp else url,
                "redirect_hops": len(resp.history) if resp else 0,
                "content_type": resp.headers.get("Content-Type", "") if resp else "",
            }

            if err:
                self.findings.append(Finding(
                    "Links", "critical", url, "page fetch",
                    f"Page could not be fetched: {err}",
                    "Confirm the URL is correct and the server is reachable."
                ))
                self.visited_pages[url] = page_record
                continue

            if resp.status_code >= 400:
                self.findings.append(Finding(
                    "Links", "critical", url, "page",
                    f"Page returned HTTP {resp.status_code}.",
                    "Fix the broken page or remove/redirect links pointing to it."
                ))
                self.visited_pages[url] = page_record
                continue

            if page_record["redirect_hops"] >= 3:
                self.findings.append(Finding(
                    "Links", "should-fix", url, "redirect chain",
                    f"{page_record['redirect_hops']} redirect hops before landing on {resp.url}.",
                    "Point the original link directly at the final destination."
                ))

            is_html = "text/html" in page_record["content_type"]
            if not is_html:
                self.visited_pages[url] = page_record
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            page_record["soup_ok"] = True
            # Stash every raw <a href> on this page once, so the link-integrity pass
            # (check_all_links) doesn't need to re-fetch and re-parse the page later.
            page_record["hrefs"] = [a["href"].strip() for a in soup.find_all("a", href=True) if a["href"].strip()]
            self.visited_pages[url] = page_record

            self.analyze_page(url, resp, soup)

            # enqueue internal links
            for href in page_record["hrefs"]:
                if href.startswith("#") or href.startswith("javascript:"):
                    continue
                absolute = urljoin(url, href)
                absolute_norm = normalize_url(absolute)
                scheme = urlparse(absolute_norm).scheme
                if scheme not in ("http", "https"):
                    continue
                if urlparse(absolute_norm).netloc == start_domain_netloc:
                    self.internal_link_targets.add(absolute_norm)
                    if absolute_norm not in seen and len(seen) < self.max_pages * 3:
                        seen.add(absolute_norm)
                        queue.append(absolute_norm)

        # orphan page check: pages we visited that nothing internally links to (besides start)
        for url in self.visited_pages:
            if url == self.start_url:
                continue
            if url not in self.internal_link_targets:
                self.findings.append(Finding(
                    "SEO", "minor", url, "internal linking",
                    "No internal link points to this page (orphan page).",
                    "Add an internal link from a relevant page, or confirm this page should be excluded from navigation."
                ))

        # duplicate titles / descriptions across pages
        for title, pages in self.title_map.items():
            if title and len(pages) > 1:
                self.findings.append(Finding(
                    "SEO", "critical", ", ".join(pages), "title tag",
                    f'Duplicate title tag "{title}" used on {len(pages)} pages.',
                    "Write a unique, descriptive title for each page."
                ))
        for desc, pages in self.desc_map.items():
            if desc and len(pages) > 1:
                self.findings.append(Finding(
                    "SEO", "should-fix", ", ".join(pages), "meta description",
                    f"Duplicate meta description used on {len(pages)} pages.",
                    "Write a unique meta description per page."
                ))

        # duplicate analytics installs (same GA/GTM id appearing more than once on one page)
        self.check_duplicate_tags()

        # UTM casing / typo consistency across whole crawl
        self.check_utm_consistency()

        # site-wide checks (sitemap, robots, 404 page)
        self.check_sitemap_and_robots()
        self.check_custom_404()

        # link integrity for every discovered link (internal + external), sample-limited
        self.check_all_links()

        return self

    # ---------- per-page analysis ----------

    def analyze_page(self, url, resp, soup):
        # SEO: title
        title_tag = soup.find("title")
        title_text = title_tag.get_text(strip=True) if title_tag else ""
        if not title_text:
            self.findings.append(Finding("SEO", "critical", url, "<title>", "Missing title tag.", "Add a unique, descriptive <title>."))
        else:
            self.title_map[title_text].append(url)
            if len(title_text) < 30 or len(title_text) > 70:
                self.findings.append(Finding(
                    "SEO", "minor", url, "<title>",
                    f"Title length is {len(title_text)} characters (ideal 50-60).",
                    "Aim for a title tag between 50 and 60 characters."
                ))

        # meta description
        meta_desc = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        desc_text = meta_desc.get("content", "").strip() if meta_desc else ""
        if not desc_text:
            self.findings.append(Finding("SEO", "should-fix", url, "meta description", "Missing meta description.", "Add a unique 150-160 character meta description."))
        else:
            self.desc_map[desc_text].append(url)
            if len(desc_text) < 100 or len(desc_text) > 170:
                self.findings.append(Finding(
                    "SEO", "minor", url, "meta description",
                    f"Meta description length is {len(desc_text)} characters (ideal 150-160).",
                    "Adjust length to roughly 150-160 characters."
                ))

        # canonical
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
        if not canonical or not canonical.get("href"):
            self.findings.append(Finding("SEO", "minor", url, "canonical tag", "Missing canonical tag.", "Add <link rel=\"canonical\" href=\"...\"> pointing to the preferred URL."))
        else:
            canon_href = urljoin(url, canonical["href"])
            if normalize_url(canon_href) != normalize_url(url) and normalize_url(canon_href) not in (url,):
                # informational, not necessarily wrong (could be intentional for params), keep as minor
                pass

        # headings
        headings = []
        for level in range(1, 7):
            for h in soup.find_all(f"h{level}"):
                headings.append((level, h.get_text(strip=True)))
        # H1 structure is filed under the SEO category (it's a ranking signal) but its dimension
        # is Accessibility: screen readers rely on exactly one H1 to know a page's main topic,
        # regardless of whether the page is indexed at all.
        h1s = [h for h in headings if h[0] == 1]
        if len(h1s) == 0:
            self.findings.append(Finding("SEO", "critical", url, "<h1>", "No H1 found on page.", "Add exactly one H1 describing the page's main topic.", dimension="Accessibility"))
        elif len(h1s) > 1:
            self.findings.append(Finding("SEO", "should-fix", url, "<h1>", f"{len(h1s)} H1 tags found on page.", "Use exactly one H1 per page; convert extras to H2/H3.", dimension="Accessibility"))
        prev_level = None
        for level, text in headings:
            if prev_level is not None and level - prev_level > 1:
                self.findings.append(Finding(
                    "Accessibility", "should-fix", url, f"<h{level}>",
                    f"Heading level jumps from H{prev_level} to H{level} ({text[:40]!r}), skipping a level.",
                    "Use consecutive heading levels (don't skip, e.g. H2 straight to H4)."
                ))
            prev_level = level

        # open graph (only flag if page looks shareable: has article/product-like content or is homepage)
        og_title = soup.find("meta", property="og:title")
        og_desc = soup.find("meta", property="og:description")
        og_image = soup.find("meta", property="og:image")
        if not (og_title and og_desc and og_image):
            # Filed as SEO category but the dimension is UX: link previews render from these tags
            # any time someone shares the URL, independent of whether a search engine is allowed
            # to index the page at all.
            self.findings.append(Finding(
                "SEO", "minor", url, "Open Graph tags",
                "Missing one or more Open Graph tags (og:title, og:description, og:image).",
                "Add og:title, og:description, and og:image so shared links render properly on social platforms.",
                dimension="UX"
            ))

        # schema / JSON-LD validity
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                self.findings.append(Finding(
                    "SEO", "should-fix", url, "JSON-LD schema",
                    "Structured data (JSON-LD) block is not valid JSON.",
                    "Fix the JSON-LD syntax or validate with a structured data testing tool."
                ))

        # embedded mixed content: an http:// resource actually loaded BY an https:// page
        # (image/script/stylesheet/iframe). This is the case browsers actually warn about or
        # block - unlike a plain <a href="http://..."> outbound link, which just opens a new
        # page and isn't blocked by anything. Keeping these separate avoids both crying wolf on
        # harmless outbound links and under-reporting real mixed-content problems.
        if urlparse(url).scheme == "https":
            embed_tags = (
                [(t, t.get("src")) for t in soup.find_all(["img", "script", "iframe"]) if t.get("src")]
                + [(t, t.get("href")) for t in soup.find_all("link", rel=lambda v: v and "stylesheet" in v) if t.get("href")]
            )
            for tag, ref in embed_tags:
                if urlparse(urljoin(url, ref)).scheme == "http":
                    self.findings.append(Finding(
                        "Site Hygiene", "should-fix", url, f"<{tag.name}> {ref[:60]}",
                        f"Page is loaded over HTTPS but embeds an HTTP {tag.name} resource - real mixed content.",
                        "Serve this resource over HTTPS, or the browser will block or warn on it.",
                        dimension="Security"
                    ))

        # viewport meta (mobile)
        viewport = soup.find("meta", attrs={"name": "viewport"})
        if not viewport:
            self.findings.append(Finding("Site Hygiene", "should-fix", url, "<meta name=viewport>", "Missing viewport meta tag.", "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> for mobile responsiveness."))

        # favicon
        favicon = soup.find("link", rel=lambda v: v and any("icon" in r.lower() for r in v))
        if not favicon:
            self.findings.append(Finding("Site Hygiene", "minor", url, "favicon", "No favicon link found.", "Add a <link rel=\"icon\"> tag pointing to a favicon file."))

        # lang attribute
        html_tag = soup.find("html")
        if not html_tag or not html_tag.get("lang"):
            self.findings.append(Finding("Accessibility", "should-fix", url, "<html lang>", "Missing lang attribute on <html>.", "Add lang=\"en\" (or appropriate language code) to the <html> tag."))

        # staging / placeholder leakage
        page_text_lower = soup.get_text(" ", strip=True).lower()
        if "lorem ipsum" in page_text_lower:
            self.findings.append(Finding("Site Hygiene", "critical", url, "page content", "Placeholder 'lorem ipsum' text found in live content.", "Replace placeholder text with real content."))
        for kw in STAGING_KEYWORDS:
            if kw.strip(".-") in url.lower():
                pass  # handled in link checks instead

        # analytics tags (GA4 / gtag / GTM)
        html_str = str(soup)
        for m in re.finditer(r"(G-[A-Z0-9]{6,10})", html_str):
            self.ga_ids_seen[m.group(1)].append(url)
        for m in re.finditer(r"(GTM-[A-Z0-9]{4,8})", html_str):
            self.ga_ids_seen[m.group(1)].append(url)

        # images: alt text + lazy loading
        images = soup.find_all("img")
        for idx, img in enumerate(images):
            src = img.get("src", "")
            filename = src.rsplit("/", 1)[-1].split("?")[0]
            filename_stem = re.sub(r"\.(png|jpe?g|gif|svg|webp)$", "", filename, flags=re.I)
            alt = img.get("alt")
            if alt is None:
                self.findings.append(Finding(
                    "Accessibility", "critical", url, f"<img src='{src[:60]}'>",
                    "Image has no alt attribute.",
                    "Add alt text describing the image, or alt=\"\" if purely decorative."
                ))
            elif alt.strip().lower() in GENERIC_ALT_PHRASES:
                self.findings.append(Finding(
                    "Accessibility", "should-fix", url, f"<img src='{src[:60]}'>",
                    f"Alt text is generic/uninformative: \"{alt}\".",
                    "Write descriptive alt text specific to what the image shows."
                ))
            elif alt.strip() and (
                re.search(r"\.(png|jpe?g|gif|svg|webp)$", alt.strip(), re.I)
                or alt.strip().lower() == filename.lower()
                or (filename_stem and alt.strip().lower() == filename_stem.lower())
            ):
                # The alt text is literally the image's filename. Screen readers read this aloud
                # verbatim - it's worse than no alt text because it sounds like real content
                # while conveying nothing. GENERIC_ALT_PHRASES above only catches generic *words*;
                # this catches the separate, very common pattern of copy-pasted filenames left in
                # the alt attribute.
                self.findings.append(Finding(
                    "Accessibility", "should-fix", url, f"<img src='{src[:60]}'>",
                    f"Alt text is the image's filename, not a description: \"{alt}\".",
                    "Replace with real descriptive text (e.g. \"Team photo at the 2024 offsite\" instead of \"team-photo-final-v2.png\")."
                ))
            elif alt is not None and alt.strip() == "":
                # Empty alt is the *correct* way to mark a purely decorative image - but many
                # website builders and CMS platforms also apply it as a default on every image
                # regardless of content, so it's frequently left in place on images that actually
                # convey information (a photo of a person, a chart, a logo used as evidence rather
                # than decoration). We can't know intent for certain from markup alone, so this is
                # a minor "verify" flag rather than a should-fix/critical claim - skip the common
                # false-positive case of small square icons, which are almost always genuinely
                # decorative.
                width = img.get("width")
                height = img.get("height")
                looks_like_small_icon = False
                if width and height:
                    try:
                        looks_like_small_icon = int(float(width)) <= 80 and int(float(height)) <= 80
                    except ValueError:
                        looks_like_small_icon = False
                if not looks_like_small_icon:
                    self.findings.append(Finding(
                        "Accessibility", "minor", url, f"<img src='{src[:60]}'>",
                        "Image has empty alt text (alt=\"\"). Correct if purely decorative, but worth a manual check.",
                        "If this image conveys real information (a photo, chart, or logo used as evidence), give it a real description; leave alt=\"\" only if it's pure decoration."
                    ))
            if idx >= 3 and img.get("loading") != "lazy":
                self.findings.append(Finding(
                    "Site Hygiene", "minor", url, f"<img src='{src[:60]}'>",
                    "Below-the-fold image is not lazy-loaded.",
                    "Add loading=\"lazy\" to images that appear below the fold."
                ))

        # forms: label association
        for form in soup.find_all("form"):
            for inp in form.find_all(["input", "textarea", "select"]):
                itype = (inp.get("type") or "").lower()
                if itype in ("hidden", "submit", "button", "reset"):
                    continue
                input_id = inp.get("id")
                has_label = False
                if input_id:
                    has_label = soup.find("label", attrs={"for": input_id}) is not None
                if not has_label and (inp.get("aria-label") or inp.get("aria-labelledby")):
                    has_label = True
                if not has_label:
                    self.findings.append(Finding(
                        "Accessibility", "critical", url, f"<{inp.name} name='{inp.get('name','')}'>",
                        "Form field has no associated label (no <label for>, aria-label, or aria-labelledby).",
                        "Associate every input with a <label for=\"id\">, or add an aria-label."
                    ))

        # generic link text
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if text in GENERIC_LINK_TEXT:
                self.findings.append(Finding(
                    "Accessibility", "should-fix", url, f"<a href='{a['href'][:60]}'>",
                    f'Link text is generic: "{a.get_text(strip=True)}".',
                    "Use descriptive link text that makes sense out of context (e.g. \"See pricing plans\" instead of \"click here\").'
                ))

        # UTM extraction from links on this page
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full = urljoin(url, href)
            query = urlsplit(full).query
            if not query or "utm_" not in query.lower():
                continue
            self.check_utm_link(url, full, query)

    # ---------- UTM ----------

    def check_utm_link(self, source_page, link_url, query_string):
        pairs = parse_qsl(query_string, keep_blank_values=True)
        seen_params_lower = set()
        for raw_key, value in pairs:
            key_lower = raw_key.lower()
            if not key_lower.startswith("utm_"):
                continue
            self.utm_param_variants[key_lower].add(raw_key)
            self.utm_values_seen[key_lower].add(value)

            if key_lower in seen_params_lower:
                self.findings.append(Finding(
                    "UTM", "should-fix", source_page, link_url,
                    f"Duplicate '{raw_key}' parameter on the same link.",
                    "Remove the duplicate UTM parameter."
                ))
            seen_params_lower.add(key_lower)

            # typo detection against canonical params
            if key_lower not in CANONICAL_UTM_PARAMS:
                close = difflib.get_close_matches(key_lower, CANONICAL_UTM_PARAMS, n=1, cutoff=0.75)
                if close:
                    self.findings.append(Finding(
                        "UTM", "critical", source_page, link_url,
                        f"Parameter '{raw_key}' looks like a typo of '{close[0]}'.",
                        f"Rename '{raw_key}' to '{close[0]}' so analytics captures this click."
                    ))

            # casing check against configured rule
            casing_rule = self.utm_rules.get("casing")
            if casing_rule == "lower" and raw_key != raw_key.lower():
                self.findings.append(Finding(
                    "UTM", "should-fix", source_page, link_url,
                    f"Parameter '{raw_key}' should be lowercase per convention.",
                    f"Rename to '{raw_key.lower()}'."
                ))

            # allowed value check
            allowed = (self.utm_rules.get("allowed") or {}).get(key_lower)
            if allowed and value not in allowed:
                self.findings.append(Finding(
                    "UTM", "should-fix", source_page, link_url,
                    f"'{raw_key}={value}' is not in the approved value list ({', '.join(allowed)}).",
                    f"Use one of the approved values for {raw_key}."
                ))

        expected = set(CANONICAL_UTM_PARAMS[:3])  # source, medium, campaign expected as a baseline
        present = {k for k in seen_params_lower}
        missing = expected - present
        if present and missing:
            self.findings.append(Finding(
                "UTM", "should-fix", source_page, link_url,
                f"Link carries some UTM params but is missing {', '.join(sorted(missing))}.",
                "Include utm_source, utm_medium, and utm_campaign together for complete attribution."
            ))

    def check_utm_consistency(self):
        # flag casing inconsistency across the whole crawl when no explicit convention given
        if self.utm_rules.get("casing"):
            return
        for param_lower, variants in self.utm_param_variants.items():
            if len(variants) > 1:
                self.findings.append(Finding(
                    "UTM", "should-fix", "site-wide", param_lower,
                    f"Parameter name casing is inconsistent across the site: {', '.join(sorted(variants))}.",
                    "Standardize on one casing (recommended: all lowercase) for UTM parameter names."
                ))
        for param_lower, values in self.utm_values_seen.items():
            lower_map = defaultdict(set)
            for v in values:
                lower_map[v.lower()].add(v)
            for lower_v, raw_variants in lower_map.items():
                if len(raw_variants) > 1:
                    self.findings.append(Finding(
                        "UTM", "should-fix", "site-wide", param_lower,
                        f"Value casing is inconsistent for {param_lower}: {', '.join(sorted(raw_variants))}.",
                        "Standardize the casing of this value; inconsistent casing fragments analytics reporting."
                    ))

    # ---------- link integrity across whole site ----------

    def check_all_links(self):
        all_links = set()
        checked = 0
        for url, page in list(self.visited_pages.items()):
            if not page.get("soup_ok"):
                continue
            for href in page.get("hrefs", []):
                if href.startswith("#") or href.startswith("javascript:"):
                    continue
                if href.startswith("mailto:"):
                    if "@" not in href or "." not in href.split("@")[-1]:
                        self.findings.append(Finding("Links", "should-fix", url, href, "Malformed mailto link.", "Confirm the email address is valid and correctly formatted."))
                    continue
                if href.startswith("tel:"):
                    digits = re.sub(r"[^\d+]", "", href.replace("tel:", ""))
                    if len(digits) < 7:
                        self.findings.append(Finding("Links", "should-fix", url, href, "Malformed tel link.", "Confirm the phone number is valid and correctly formatted."))
                    continue

                absolute = urljoin(url, href)
                if urlparse(absolute).scheme not in ("http", "https"):
                    continue
                absolute_norm = normalize_url(absolute)

                # staging leakage
                if any(kw in absolute_norm.lower() for kw in STAGING_KEYWORDS):
                    self.findings.append(Finding("Site Hygiene", "critical", url, absolute_norm, "Link points to a staging/dev environment.", "Replace with the production URL before this link goes live."))

                # An <a href="http://..."> outbound link on an https page is NOT browser-blocked
                # mixed content (that only applies to resources the page actually loads, like
                # images/scripts - see the separate embedded-mixed-content check in analyze_page).
                # This is a much softer issue: the destination itself doesn't offer HTTPS. Don't
                # recommend "just switch to https://" unless we've actually confirmed that works.
                if urlparse(url).scheme == "https" and urlparse(absolute_norm).scheme == "http":
                    dest_netloc = urlparse(absolute_norm).netloc
                    if self.probe_https_support(dest_netloc):
                        fix_text = "Switch this link to https:// - the destination supports it."
                    else:
                        fix_text = (
                            "This destination doesn't support HTTPS at all, so the link isn't fixable on your "
                            "end. Consider removing or replacing it if it's not essential."
                        )
                    self.findings.append(Finding(
                        "Links", "minor", url, absolute_norm,
                        "Link points to an http:// (non-secure) destination.",
                        fix_text
                    ))

                if absolute_norm in all_links:
                    continue
                all_links.add(absolute_norm)

                is_internal = urlparse(absolute_norm).netloc.lstrip("www.") == urlparse(self.start_url).netloc.lstrip("www.")
                if not is_internal and checked >= self.max_external_checks:
                    continue
                if not is_internal:
                    checked += 1

                status, final_url, hops, err2 = self.check_link_status(absolute_norm)
                if err2:
                    self.findings.append(Finding("Links", "should-fix" if not is_internal else "critical", url, absolute_norm, f"Link could not be reached: {err2}", "Verify the URL is correct and the destination is online."))
                elif status and status >= 400:
                    sev = "critical" if is_internal else "should-fix"
                    self.findings.append(Finding("Links", sev, url, absolute_norm, f"Link returns HTTP {status}.", "Fix the destination, update the link, or remove it."))
                elif hops and hops >= 3:
                    self.findings.append(Finding("Links", "should-fix", url, absolute_norm, f"Redirect chain of {hops} hops to {final_url}.", "Point the link directly at the final destination."))

    # ---------- site-wide checks ----------

    def check_duplicate_tags(self):
        for tag_id, pages in self.ga_ids_seen.items():
            counts = defaultdict(int)
            for p in pages:
                counts[p] += 1
            dupe_pages = [p for p, c in counts.items() if c > 1]
            for p in dupe_pages:
                self.findings.append(Finding(
                    "Site Hygiene", "critical", p, tag_id,
                    f"Analytics tag {tag_id} appears to be installed more than once on this page.",
                    "Remove the duplicate analytics/tag manager snippet to avoid double-counting conversions.",
                    dimension="Analytics"
                ))

    def check_sitemap_and_robots(self):
        base = f"{urlparse(self.start_url).scheme}://{urlparse(self.start_url).netloc}"
        sitemap_url = urljoin(base, "/sitemap.xml")
        resp, err = self.fetch(sitemap_url)
        if err or not resp or resp.status_code >= 400:
            self.findings.append(Finding("SEO", "should-fix", sitemap_url, "sitemap.xml", "sitemap.xml not found or unreachable.", "Add a sitemap.xml listing all indexable pages and submit it in Search Console."))

        robots_url = urljoin(base, "/robots.txt")
        resp, err = self.fetch(robots_url)
        if err or not resp or resp.status_code >= 400:
            self.findings.append(Finding("SEO", "minor", robots_url, "robots.txt", "robots.txt not found or unreachable.", "Add a robots.txt file, even if it just allows all crawling."))
        elif "Disallow: /" in resp.text and "Disallow: /\n" in resp.text:
            self.findings.append(Finding("SEO", "critical", robots_url, "robots.txt", "robots.txt disallows the entire site.", "Confirm this is intentional; otherwise remove the blanket Disallow: / rule."))

    def check_custom_404(self):
        base = f"{urlparse(self.start_url).scheme}://{urlparse(self.start_url).netloc}"
        nonsense = urljoin(base, "/this-page-should-not-exist-qa-check-8842")
        resp, err = self.fetch(nonsense)
        if err or not resp:
            return
        if resp.status_code != 404:
            self.findings.append(Finding(
                "Site Hygiene", "should-fix", nonsense, "404 handling",
                f"Nonexistent URL returned HTTP {resp.status_code} instead of 404.",
                "Configure the server to return a proper 404 status for missing pages."
            ))
        else:
            body_lower = resp.text.lower()
            if "<nav" not in body_lower and "home" not in body_lower:
                self.findings.append(Finding(
                    "Site Hygiene", "minor", nonsense, "404 page",
                    "404 page may lack navigation back to the site (no obvious nav/home link detected).",
                    "Add navigation, search, or a link to the homepage on the 404 page."
                ))

    # ---------- reporting ----------

    def sorted_findings(self):
        findings = self.findings
        if self.skip_seo:
            findings = [f for f in findings if f.dimension != "SEO"]
        return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 3), f.category, f.page))

    def write_reports(self, out_dir, prefix):
        ts = now_ts()
        findings = self.sorted_findings()
        counts = defaultdict(int)
        for f in findings:
            counts[f.severity] += 1

        summary_path = f"{out_dir}/{prefix}audit_summary_{ts}.txt"
        csv_path = f"{out_dir}/{prefix}audit_findings_{ts}.csv"
        json_path = f"{out_dir}/{prefix}audit_data_{ts}.json"
        html_path = f"{out_dir}/{prefix}audit_report_{ts}.html"

        # summary txt
        with open(summary_path, "w") as f:
            f.write(f"Website QA Audit Summary\n")
            f.write(f"Start URL: {self.start_url}\n")
            f.write(f"Pages crawled: {len(self.visited_pages)}\n")
            f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n")
            f.write(f"Critical: {counts['critical']}\n")
            f.write(f"Should-fix: {counts['should-fix']}\n")
            f.write(f"Minor: {counts['minor']}\n\n")
            for sev in ("critical", "should-fix", "minor"):
                sev_findings = [x for x in findings if x.severity == sev]
                if not sev_findings:
                    continue
                f.write(f"--- {sev.upper()} ({len(sev_findings)}) ---\n")
                for x in sev_findings[:50]:
                    f.write(f"[{x.category}/{x.dimension}] {x.page} - {x.description}\n")
                f.write("\n")

        # csv
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Category", "Dimension", "Severity", "Page", "Location", "Description", "Fix"])
            for x in findings:
                writer.writerow([x.category, x.dimension, x.severity, x.page, x.location, x.description, x.fix])

        # json
        data = {
            "start_url": self.start_url,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pages_crawled": len(self.visited_pages),
            "counts": dict(counts),
            "pages": list(self.visited_pages.keys()),
            "findings": [x.as_dict() for x in findings],
        }
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2)

        # html
        by_category = defaultdict(list)
        for x in findings:
            by_category[x.category].append(x)

        rows_html = []
        for category, items in by_category.items():
            cat_counts = defaultdict(int)
            for it in items:
                cat_counts[it.severity] += 1
            rows_html.append(f"<h2>{category} - {cat_counts['critical']} critical, {cat_counts['should-fix']} should-fix, {cat_counts['minor']} minor</h2>")
            rows_html.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%;font-family:sans-serif;font-size:14px'>")
            rows_html.append("<tr style='background:#f0f0f0'><th>Severity</th><th>Dimension</th><th>Page</th><th>Location</th><th>Description</th><th>Fix</th></tr>")
            for it in items:
                color = {"critical": "#ffe5e5", "should-fix": "#fff6e0", "minor": "#eef7ee"}.get(it.severity, "#fff")
                rows_html.append(
                    f"<tr style='background:{color}'><td>{it.severity}</td><td>{it.dimension}</td><td>{it.page}</td>"
                    f"<td>{it.location}</td><td>{it.description}</td><td>{it.fix}</td></tr>"
                )
            rows_html.append("</table>")

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Website QA Audit - {self.start_url}</title>
</head>
<body style="font-family:sans-serif;max-width:1100px;margin:40px auto;padding:0 20px">
<h1>Website QA Audit</h1>
<p><strong>Start URL:</strong> {self.start_url}<br>
<strong>Pages crawled:</strong> {len(self.visited_pages)}<br>
<strong>Generated:</strong> {datetime.now(timezone.utc).isoformat()}</p>
<p><strong>Critical:</strong> {counts['critical']} &nbsp; <strong>Should-fix:</strong> {counts['should-fix']} &nbsp; <strong>Minor:</strong> {counts['minor']}</p>
{''.join(rows_html)}
</body>
</html>"""
        with open(html_path, "w") as f:
            f.write(html_content)

        return {
            "summary": summary_path,
            "csv": csv_path,
            "json": json_path,
            "html": html_path,
            "counts": dict(counts),
        }


def main():
    parser = argparse.ArgumentParser(description="Website QA & technical audit crawler.")
    parser.add_argument("url", help="Starting URL (homepage or any page).")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum number of pages to crawl (default 50).")
    parser.add_argument("--wcag", choices=["AA", "AAA"], default="AA", help="WCAG accessibility level (default AA).")
    parser.add_argument("--utm-config", default=None, help="Path to a JSON file describing UTM naming conventions.")
    parser.add_argument("--out-dir", default=".", help="Directory to write report files to (default current directory).")
    parser.add_argument("--prefix", default="", help="Optional filename prefix, e.g. 'mysite_'.")
    parser.add_argument("--max-external-checks", type=int, default=150, help="Cap on how many external links get status-checked.")
    parser.add_argument("--timeout", type=int, default=TIMEOUT, help=f"Per-request timeout in seconds (default {TIMEOUT}).")
    parser.add_argument("--skip-seo", action="store_true",
                         help="Exclude SEO-dimension findings (duplicate titles, missing meta descriptions, "
                              "orphan pages, sitemap/robots issues) from the reports. Use this for sites where "
                              "search ranking doesn't matter (internal tools, unlaunched sites, deliberately "
                              "deindexed sites, etc.). Accessibility, UX, Security, and Analytics findings are "
                              "unaffected - those matter regardless of indexing.")
    args = parser.parse_args()

    utm_rules = {}
    if args.utm_config:
        with open(args.utm_config) as f:
            utm_rules = json.load(f)

    t0 = time.time()
    auditor = Auditor(
        args.url,
        max_pages=args.max_pages,
        wcag=args.wcag,
        utm_rules=utm_rules,
        max_external_checks=args.max_external_checks,
        timeout=args.timeout,
        skip_seo=args.skip_seo,
    )
    auditor.crawl()
    paths = auditor.write_reports(args.out_dir, args.prefix)
    elapsed = time.time() - t0

    print(f"Crawled {len(auditor.visited_pages)} pages in {elapsed:.1f}s")
    print(f"Findings - critical: {paths['counts'].get('critical', 0)}, "
          f"should-fix: {paths['counts'].get('should-fix', 0)}, "
          f"minor: {paths['counts'].get('minor', 0)}")
    print(f"Summary:  {paths['summary']}")
    print(f"CSV:      {paths['csv']}")
    print(f"JSON:     {paths['json']}")
    print(f"HTML:     {paths['html']}")


if __name__ == "__main__":
    main()
