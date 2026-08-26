#!/usr/bin/env python3
"""
verify_citations.py — check every claim and citation in a piece of AI (or
human) research writing against the actual sources they cite.

WHY THIS EXISTS
----------------
AI is genuinely good at research synthesis, but it states a wrong fact with
exactly the same confident tone as a right one, and a citation next to a
claim is not proof the claim is true. This script does what a careful editor
would do before a piece runs: go back to the real source for every claim and
check it against reality, then flag — loudly — anything that can't be
confirmed instead of quietly letting it through.

HOW IT WORKS
------------
1. EXTRACT   — send the document to an LLM and get back a structured list
               of every checkable claim + whatever citation was attached.
2. FETCH     — for each unique citation, actually retrieve the source: fetch
               the URL, read the local file, or note that neither was
               possible.
3. CLASSIFY  — for each claim with a reachable source, ask an LLM to compare
               the claim against the real source text and return one of:
               CONFIRMED, CONTRADICTED, or UNCONFIRMED. Claims with no
               citation are UNCITED; claims whose source couldn't be fetched
               are UNREACHABLE — both decided directly by this code, not by
               the model, since those are just facts about what happened,
               not judgment calls.
4. REPORT    — write a markdown report: a summary count, a claim-by-claim
               table with the actual evidence quoted, and an annotated copy
               of the original document with inline verdict markers.

PROVIDERS
---------
The LLM calls go through a small `LLMProvider` interface near the top of
this file, so this works with whatever AI you have access to:
  - Anthropic (Claude)        --provider anthropic
  - OpenAI, or anything that speaks the same chat-completions API shape —
    Azure OpenAI, OpenRouter, Groq, Together AI, or a local server like
    Ollama / LM Studio / vLLM   --provider openai --base-url <endpoint>
To plug in something else entirely (Gemini, Cohere, an in-house model),
subclass LLMProvider and implement one method: complete(system, user).

INSTALL
-------
    pip install requests beautifulsoup4 lxml anthropic openai pdfplumber
(You only actually need the SDK for the provider you use, and pdfplumber
only if a document or source is a .pdf — the rest are always required.)

USAGE
-----
    export ANTHROPIC_API_KEY=sk-...
    python verify_citations.py report.md --sources acme_report.md \\
        --output verification_report.md

Run with --help for the full flag list.
"""

from __future__ import annotations

import abc
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

MAX_SOURCE_CHARS_DEFAULT = 15000
REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "verify-citations/1.0 (fact-checking tool; https://github.com/)"


# ---------------------------------------------------------------------------
# LLM provider layer — the only part of this file that talks to an AI vendor.
# Everything else just calls provider.complete(system, user) and gets text
# back, which is what makes this usable with whichever AI you have access to.
# ---------------------------------------------------------------------------


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-5-20250929", api_key: str | None = None):
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The 'anthropic' package is required for --provider anthropic.\n"
                "Install it with: pip install anthropic"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("No Anthropic API key found. Set ANTHROPIC_API_KEY or pass --api-key.")
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in response.content if block.type == "text")


class OpenAICompatibleProvider(LLMProvider):
    """
    Works with OpenAI's own API, and with anything else that implements the
    same /chat/completions shape: Azure OpenAI, OpenRouter, Groq, Together AI,
    or a local server like Ollama (http://localhost:11434/v1), LM Studio, or
    vLLM. Point --base-url at whichever endpoint you're using.
    """

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None, base_url: str | None = None):
        try:
            import openai
        except ImportError as exc:
            raise RuntimeError(
                "The 'openai' package is required for --provider openai.\n"
                "Install it with: pip install openai"
            ) from exc
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key and not base_url:
            raise RuntimeError(
                "No OpenAI API key found. Set OPENAI_API_KEY or pass --api-key "
                "(or pass --base-url if you're pointing at a local server that doesn't need one)."
            )
        self._client = openai.OpenAI(api_key=key or "not-needed", base_url=base_url)
        self._model = model

    def complete(self, system: str, user: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return response.choices[0].message.content or ""


def build_provider(name: str, model: str | None, api_key: str | None, base_url: str | None) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(model=model or "claude-sonnet-4-5-20250929", api_key=api_key)
    if name == "openai":
        return OpenAICompatibleProvider(model=model or "gpt-4o-mini", api_key=api_key, base_url=base_url)
    raise ValueError(f"Unknown provider: {name!r}. Use 'anthropic' or 'openai'.")


# ---------------------------------------------------------------------------
# Prompts — where the actual "how strict is strict" judgment calls live.
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM_PROMPT = """You are auditing a piece of research writing before it ships. Your only \
job right now is extraction, not judgment: read the document below and pull out every checkable \
factual claim it makes — a statistic, a date, a direct quote, a named event, or a specific factual \
assertion (e.g. "X acquired Y," "the study found Z," "adoption grew by N%") — along with whatever \
citation is attached to each one.

Rules:
- Include a claim even if it has NO citation attached — set "citation" to null in that case. Missing \
a claim that turns out to be wrong is a much worse mistake than including a few extra borderline ones, \
so when in doubt, include it.
- Leave out the writer's own clearly-labeled analysis or opinion ("this suggests...", "we'd expect..."), \
generic background knowledge nobody would challenge, and pure connective/summary language.
- A vague attribution — "studies show," "reports suggest," "experts say," with nothing specific enough \
to trace — counts as having NO real citation. Set "citation" to null and set "citation_type" to "vague".
- For "quote", copy the EXACT verbatim sentence or clause from the document containing the claim. Do \
not paraphrase or clean it up — it needs to match the source text exactly, character for character, so \
it can be located again later.
- For "citation", capture the citation about as it's written: a URL, "DocumentName, Section X", "per \
[Report Name]", etc. Set it to null if there truly is none, or if it's a vague attribution.
- "citation_type" must be exactly one of: "url" (a specific web address is given), "local_document" (a \
named report/document, with or without a page/section, but no URL), "vague" (a hand-wavy attribution \
like "experts say"), or "none" (nothing at all).

Respond with ONLY a JSON array, no other commentary, in exactly this shape:
[
  {
    "quote": "<verbatim excerpt from the document>",
    "claim": "<the specific checkable fact, restated plainly if that's clearer than the quote alone>",
    "citation": "<url, or 'DocName, Section X', or null>",
    "citation_type": "url" | "local_document" | "vague" | "none"
  }
]
If there are no checkable claims at all, respond with an empty JSON array: []
"""

CLASSIFICATION_SYSTEM_PROMPT = """You are verifying one specific claim against the actual source that \
was cited for it. Be strict and skeptical — the whole point of this exercise is to catch \
confident-sounding claims that don't actually hold up, so do not be generous or fill in gaps with what \
would make the claim true.

Match strictly on figures and quotes. If a claim states a specific number, the source needs to state \
that same figure (or something from which it's the direct, unambiguous result of arithmetic you can \
show) — a source that's merely in the same ballpark, or that supports the general direction without the \
specific number, is NOT a confirmation of that specific number. Quotes need to match what the source \
actually says, not a tightened or reordered version of it.

Classify the claim into exactly one of these three verdicts (the other two possible verdicts — \
UNREACHABLE and UNCITED — are decided by the calling code before you ever see this claim, based on \
whether a source could be fetched at all, so you will never need to choose them):

- CONFIRMED: the source states this specific fact/figure/quote, or something a careful, literal reader \
  would call directly equivalent.
- CONTRADICTED: the source is on-topic and was actually read, but states something different from the \
  claim — a different number, a different attribution, an opposite conclusion, or it explicitly says it \
  doesn't cover the topic the claim attributes to it.
- UNCONFIRMED: the source was read in full (within what you were given), but genuinely does not contain \
  support for this specific claim. This is the "sounds plausible, but I can't find it in here" bucket — \
  it is often exactly where a fabricated specific lands, so don't avoid it just because the claim reads \
  smoothly.

Always quote the specific passage from the provided source text that your verdict is based on. For \
UNCONFIRMED, say plainly that no relevant passage was found, and name the closest related content if \
there is any.

Respond with ONLY a JSON object, no other commentary, in exactly this shape:
{"verdict": "CONFIRMED" | "CONTRADICTED" | "UNCONFIRMED", "evidence": "<quoted passage, or an explanation of what was and wasn't found>"}
"""


def classification_user_prompt(claim: str, citation: str, source_label: str, source_text: str) -> str:
    return (
        f"CLAIM TO CHECK:\n{claim}\n\n"
        f"CITATION GIVEN FOR IT:\n{citation}\n\n"
        f"SOURCE ({source_label}) — full text follows between the markers:\n"
        f"-----BEGIN SOURCE TEXT-----\n{source_text}\n-----END SOURCE TEXT-----\n"
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    quote: str
    claim: str
    citation: str | None
    citation_type: str  # "url" | "local_document" | "vague" | "none"
    verdict: str = ""  # CONFIRMED | CONTRADICTED | UNCONFIRMED | UNREACHABLE | UNCITED
    evidence: str = ""
    source_label: str = ""


@dataclass
class Source:
    label: str
    text: str | None  # None if it couldn't be fetched
    error: str = ""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def extract_json(text: str):
    """Pull a JSON value out of a model reply, tolerating ```json fences etc."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return json.loads(text)  # let this raise if truly unparsable


def find_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s)>\]\"']+", text)
    if not match:
        return None
    return match.group(0).rstrip(").,]\"'")


def read_local_file(path: str) -> str:
    if path.lower().endswith(".pdf"):
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError(
                "Reading PDF sources requires pdfplumber. Install it with: pip install pdfplumber"
            ) from exc
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def fetch_url(url: str) -> Source:
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        return Source(label=url, text=None, error=str(exc))

    soup = BeautifulSoup(response.text, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()
    if not text:
        return Source(label=url, text=None, error="Page fetched but contained no readable text (may require JavaScript).")
    return Source(label=url, text=text)


def match_local_source(citation: str, source_paths: list[str]) -> str | None:
    """
    Best-effort match of a named-document citation ("Acme Market Intelligence
    Report, Q1 2026, Section 3.2") to one of the --sources files.

    With exactly one file passed, we use it — most users citing "the report"
    only have one candidate in mind anyway. With multiple files, we score
    each by token overlap between the citation and BOTH the filename and a
    chunk of the file's own content, since a citation rarely repeats a
    file's literal filename but often repeats words that appear in the
    document itself (a title, a company name, a heading).
    """

    def tokens(s: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2}

    if not source_paths:
        return None
    if len(source_paths) == 1:
        return source_paths[0]

    citation_tokens = tokens(citation)
    if not citation_tokens:
        return None

    best_path, best_score = None, 0
    for path in source_paths:
        basename = os.path.splitext(os.path.basename(path))[0]
        score = 2 * len(citation_tokens & tokens(basename))
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                snippet = f.read(2000)
            score += len(citation_tokens & tokens(snippet))
        except OSError:
            pass
        if score > best_score:
            best_path, best_score = path, score

    return best_path if best_score > 0 else None


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def extract_claims(provider: LLMProvider, document_text: str) -> list[Claim]:
    reply = provider.complete(EXTRACTION_SYSTEM_PROMPT, document_text)
    raw = extract_json(reply)
    claims = []
    for item in raw:
        claims.append(
            Claim(
                quote=item.get("quote", "").strip(),
                claim=item.get("claim", item.get("quote", "")).strip(),
                citation=item.get("citation") or None,
                citation_type=item.get("citation_type", "none"),
            )
        )
    return claims


def resolve_sources(claims: list[Claim], source_paths: list[str], max_source_chars: int) -> dict[str, Source]:
    """Fetch/read each unique citation once, return {citation_key: Source}."""
    cache: dict[str, Source] = {}

    for claim in claims:
        if claim.citation_type in ("vague", "none") or not claim.citation:
            continue

        key = claim.citation
        if key in cache:
            continue

        if claim.citation_type == "url" or find_url(claim.citation):
            url = find_url(claim.citation) or claim.citation
            print(f"  fetching {url} ...", file=sys.stderr)
            source = fetch_url(url)
        else:
            matched_path = match_local_source(claim.citation, source_paths)
            if matched_path:
                print(f"  reading {matched_path} (matched to citation: {claim.citation!r}) ...", file=sys.stderr)
                try:
                    source = Source(label=matched_path, text=read_local_file(matched_path))
                except Exception as exc:  # noqa: BLE001 - surfaced as an Unreachable reason
                    source = Source(label=matched_path, text=None, error=str(exc))
            else:
                source = Source(
                    label=claim.citation,
                    text=None,
                    error=(
                        "Named a document/report but no matching file was passed via --sources, "
                        "and no URL was given. This tool doesn't do open-ended web search on your "
                        "behalf — pass the source file, or add its URL to the citation."
                    ),
                )

        if source.text and len(source.text) > max_source_chars:
            source.text = source.text[:max_source_chars] + "\n\n[...truncated for length...]"

        cache[key] = source

    return cache


def classify_claims(provider: LLMProvider, claims: list[Claim], sources: dict[str, Source]) -> None:
    for claim in claims:
        if claim.citation_type in ("vague", "none") or not claim.citation:
            claim.verdict = "UNCITED"
            claim.evidence = (
                "No specific, traceable source was given for this claim."
                if claim.citation_type == "none"
                else f"Attribution given ({claim.citation!r}) is too vague to trace to a specific source."
            )
            continue

        source = sources.get(claim.citation)
        if source is None or source.text is None:
            claim.verdict = "UNREACHABLE"
            claim.evidence = source.error if source else "Source could not be resolved."
            claim.source_label = source.label if source else claim.citation
            continue

        claim.source_label = source.label
        print(f"  classifying: {claim.claim[:70]}...", file=sys.stderr)
        reply = provider.complete(
            CLASSIFICATION_SYSTEM_PROMPT,
            classification_user_prompt(claim.claim, claim.citation, source.label, source.text),
        )
        try:
            result = extract_json(reply)
            claim.verdict = result.get("verdict", "UNCONFIRMED").upper()
            claim.evidence = result.get("evidence", "")
        except (json.JSONDecodeError, AttributeError):
            claim.verdict = "UNCONFIRMED"
            claim.evidence = f"Could not parse model response as JSON. Raw reply: {reply[:300]}"


# ---------------------------------------------------------------------------
# Report building
# ---------------------------------------------------------------------------

VERDICT_MARK = {
    "CONFIRMED": "✓ Confirmed",
    "CONTRADICTED": "✗ Contradicted",
    "UNCONFIRMED": "⚠ Unconfirmed",
    "UNREACHABLE": "? Unreachable",
    "UNCITED": "✎ Uncited",
}


def build_report(document_text: str, claims: list[Claim], document_label: str) -> str:
    counts = {v: 0 for v in VERDICT_MARK}
    for c in claims:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    total = len(claims)
    non_confirmed = total - counts.get("CONFIRMED", 0)

    lines = []
    lines.append("# Citation Verification Report")
    lines.append("")
    lines.append(f"**Source material:** {document_label}")
    lines.append(f"**Claims checked:** {total}")
    lines.append("")
    lines.append("| Verdict | Count |")
    lines.append("|---|---|")
    for key, label in VERDICT_MARK.items():
        lines.append(f"| {label} | {counts.get(key, 0)} |")
    lines.append("")
    if total == 0:
        lines.append("No checkable claims were found in this document.")
    elif non_confirmed == 0:
        lines.append(f"**All {total} claims checked out.** No flags raised.")
    else:
        lines.append(
            f"**{non_confirmed} of {total} claims need attention before this ships** "
            f"({counts.get('CONTRADICTED', 0)} contradicted, {counts.get('UNCONFIRMED', 0)} unconfirmed, "
            f"{counts.get('UNREACHABLE', 0)} unreachable, {counts.get('UNCITED', 0)} uncited)."
        )
    lines.append("")
    lines.append("### Claim-by-claim detail")
    lines.append("")
    lines.append("| # | Claim | Citation given | Verdict | Evidence found |")
    lines.append("|---|---|---|---|---|")
    for i, c in enumerate(claims, start=1):
        claim_txt = c.claim.replace("|", "\\|").replace("\n", " ")
        citation_txt = (c.citation or "none given").replace("|", "\\|")
        evidence_txt = c.evidence.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | {claim_txt} | {citation_txt} | {VERDICT_MARK.get(c.verdict, c.verdict)} | {evidence_txt} |")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Annotated copy")
    lines.append("")
    annotated = document_text
    # Longest quotes first, so a short quote that's a substring of a longer
    # one doesn't get annotated in the wrong place.
    for c in sorted(claims, key=lambda c: -len(c.quote)):
        if not c.quote:
            continue
        marker = f" **[{VERDICT_MARK.get(c.verdict, c.verdict)}]**"
        if c.quote in annotated:
            annotated = annotated.replace(c.quote, c.quote + marker, 1)
        else:
            lines.append(
                f"\n> Could not locate this claim's exact wording in the original text to annotate it "
                f"inline: \"{c.quote}\"{marker}"
            )
    lines.append("")
    lines.append(annotated)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify every claim and citation in a document against its actual sources.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("document", help="Path to the document to verify (.md, .txt, or .pdf)")
    parser.add_argument(
        "--sources",
        nargs="*",
        default=[],
        help="Local files that correspond to named-document citations (.md, .txt, .pdf). "
        "URLs in citations are fetched automatically and don't need to be listed here.",
    )
    parser.add_argument("--provider", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--model", default=None, help="Override the default model for the chosen provider.")
    parser.add_argument("--api-key", default=None, help="API key (otherwise read from the provider's env var).")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Custom API base URL — use with --provider openai to point at Azure OpenAI, "
        "OpenRouter, Ollama, LM Studio, vLLM, etc.",
    )
    parser.add_argument("--output", "-o", default=None, help="Where to write the report (default: stdout).")
    parser.add_argument(
        "--max-source-chars",
        type=int,
        default=MAX_SOURCE_CHARS_DEFAULT,
        help="Truncate very long fetched sources to this many characters before sending them to the model.",
    )
    args = parser.parse_args()

    document_text = read_local_file(args.document)
    provider = build_provider(args.provider, args.model, args.api_key, args.base_url)

    print("Extracting claims...", file=sys.stderr)
    claims = extract_claims(provider, document_text)
    print(f"Found {len(claims)} checkable claim(s). Resolving sources...", file=sys.stderr)

    sources = resolve_sources(claims, args.sources, args.max_source_chars)

    print("Classifying claims against their sources...", file=sys.stderr)
    classify_claims(provider, claims, sources)

    report = build_report(document_text, claims, document_label=os.path.basename(args.document))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
