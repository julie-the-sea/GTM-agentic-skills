#!/usr/bin/env python3
"""
Editing Assistant
==================

A platform-agnostic content polish workflow. This script does NOT call any
AI API itself -- it has no dependencies and needs no API key or billing
account. Instead, at each step it prints a ready-to-paste prompt, you paste
that prompt into whatever AI chat you already use (ChatGPT, Claude.ai,
Gemini, Copilot, etc.), and you paste the AI's reply back into this script.
The script handles everything else: intake rules, gating, source citation
bookkeeping, brand-voice profile reuse, the review/approve workflow, and
writing the final output files.

Why this shape: it costs nothing to run beyond whatever AI subscription you
already have, needs no developer account, and works identically regardless
of which AI product your team uses -- which matters most for teams without
budget for a dedicated API integration.

Run it with:
    python3 editing_assistant.py

No third-party packages required (standard library only).
"""

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

APP_NAME = "Editing Assistant"
DIVIDER = "=" * 78
THIN = "-" * 78
PASTE_END_MARKER = "END"


# ---------------------------------------------------------------------------
# Small terminal I/O helpers
# ---------------------------------------------------------------------------

def banner(text: str) -> None:
    print()
    print(DIVIDER)
    print(text)
    print(DIVIDER)


def section(text: str) -> None:
    print()
    print(THIN)
    print(text)
    print(THIN)


def ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("  (this can't be blank -- please enter a value)")


def confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} ({hint}): ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  please answer y or n")


def choose(prompt: str, options: List[str]) -> int:
    """Print numbered options and return the 0-based index the user picked."""
    print(prompt)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        answer = input(f"Enter a number (1-{len(options)}): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print("  please enter a valid number from the list")


def ask_optional_path(prompt: str) -> Optional[Path]:
    answer = input(f"{prompt} (or press Enter to skip): ").strip()
    if not answer:
        return None
    p = Path(answer).expanduser()
    while not p.is_file():
        print(f"  Couldn't find a file at: {p}")
        answer = input(f"{prompt} (or press Enter to skip): ").strip()
        if not answer:
            return None
        p = Path(answer).expanduser()
    return p


def ask_required_path(prompt: str) -> Path:
    while True:
        answer = input(f"{prompt}: ").strip()
        if not answer:
            print("  This file is required to continue.")
            continue
        p = Path(answer).expanduser()
        if p.is_file():
            return p
        print(f"  Couldn't find a file at: {p}")


def read_file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def read_multiline(prompt: str) -> str:
    """
    Read a multi-line paste from the user. The user pastes the AI's
    response, then types END on its own line and presses Enter.
    """
    print(prompt)
    print(f"(Paste the AI's full response below. When you're done, type "
          f"{PASTE_END_MARKER} on its own line and press Enter.)")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == PASTE_END_MARKER:
            break
        lines.append(line)
    return "\n".join(lines)


def print_prompt_block(prompt_text: str) -> None:
    print()
    print(">>> COPY EVERYTHING BETWEEN THE LINES BELOW AND PASTE IT INTO YOUR AI CHAT >>>")
    print(THIN)
    print(prompt_text)
    print(THIN)
    print("<<< END OF PROMPT TO COPY <<<")
    print()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Flag:
    id: int
    category: str          # mechanical | voice | readability | seo
    original: str
    suggestion: str
    source: str             # e.g. "Voice doc, rule 4" or "Inferred rule 2"
    reason: str = ""
    decision: Optional[str] = None   # "accept" | "reject"

    def label(self) -> str:
        return {
            "mechanical": "MECHANICAL",
            "voice": "VOICE",
            "readability": "READABILITY",
            "seo": "SEO",
        }.get(self.category, self.category.upper())


CATEGORY_ORDER = ["mechanical", "voice", "readability", "seo"]
CATEGORY_TITLES = {
    "mechanical": "Step 1 - Mechanical pass (grammar, spelling, punctuation)",
    "voice": "Step 2 - Brand voice conformance",
    "readability": "Step 3 - Readability pass",
    "seo": "Step 4 - Keyword/SEO pass",
}


# ---------------------------------------------------------------------------
# JSON extraction from pasted AI responses
# ---------------------------------------------------------------------------

def extract_json_array(raw_text: str):
    """
    Try hard to find a JSON array in whatever the user pasted, even if the
    AI added commentary before/after the array or wrapped it in a code
    fence. Returns (list_or_None, error_message_or_None).
    """
    text = raw_text.strip()

    # Strip common code-fence wrapping.
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
    text = re.sub(r"```$", "", text.strip()).strip()

    # First, try a direct parse.
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data, None
    except json.JSONDecodeError:
        pass

    # Fall back to slicing between the first '[' and the last ']'.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data, None
        except json.JSONDecodeError as e:
            return None, f"Found something that looks like a list but it didn't parse cleanly ({e})."

    return None, "No JSON array found in the pasted text."


def flags_from_json(data, category: str, start_id: int) -> List[Flag]:
    flags = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        flags.append(Flag(
            id=start_id + i,
            category=category,
            original=str(item.get("original", "")).strip(),
            suggestion=str(item.get("suggestion", "")).strip(),
            source=str(item.get("source", "")).strip() or "(source not given)",
            reason=str(item.get("reason", "")).strip(),
        ))
    return flags


def collect_flags_from_paste(category: str, start_id: int) -> List[Flag]:
    """Loop: read a paste, try to parse it as JSON, retry on failure."""
    while True:
        raw = read_multiline(f"Waiting for the AI's response for: {CATEGORY_TITLES[category]}")
        if not raw.strip():
            print("  Nothing was pasted. Let's try again.")
            continue
        # Allow the user to say the AI found nothing.
        if raw.strip().lower() in ("[]", "none", "no issues", "no flags"):
            return []
        data, err = extract_json_array(raw)
        if data is None:
            print(f"  Couldn't read that as a JSON list: {err}")
            print("  You can paste the response again (make sure it's the raw JSON "
                  "array, e.g. starting with '[' and ending with ']'), "
                  "or type SKIP to record zero flags for this pass.")
            retry = input("  Paste again, or type SKIP: ").strip()
            if retry.upper() == "SKIP":
                return []
            # push the retry back as if it were the start of a new paste
            data, err = extract_json_array(retry)
            if data is None:
                print("  Still couldn't parse that -- skipping this pass with zero flags.")
                return []
        return flags_from_json(data, category, start_id)


# ---------------------------------------------------------------------------
# Brand voice profile persistence
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "default"


def profile_path(brand_slug: str) -> Path:
    return Path(f"editing_assistant_voice_profile_{brand_slug}.json")


def load_profile(brand_slug: str) -> Optional[dict]:
    p = profile_path(brand_slug)
    if p.is_file():
        try:
            return json.loads(read_file_text(p))
        except json.JSONDecodeError:
            return None
    return None


def save_profile(brand_slug: str, rules: List[str], sample_files: List[str]) -> Path:
    p = profile_path(brand_slug)
    payload = {
        "brand": brand_slug,
        "rules": rules,
        "derived_from_samples": sample_files,
        "last_updated": datetime.now().isoformat(timespec="seconds"),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

JSON_FORMAT_INSTRUCTIONS = """
Respond with ONLY a raw JSON array (no commentary, no markdown code fences,
no leading/trailing text). Each element must be an object with exactly
these keys:

  "original":   the exact original text being flagged (short, quotable)
  "suggestion": your proposed replacement text
  "source":     what rule or reasoning justifies this flag (see instructions above)
  "reason":     one short sentence explaining the issue

If you find nothing to flag, respond with exactly: []
"""


def build_voice_extraction_prompt(samples_text: str) -> str:
    return f"""You are analyzing brand voice from long-form writing samples.

Read the following writing samples from one company and infer the implicit
rules governing its brand voice: sentence rhythm, point of view, formatting
habits, recurring phrases, and things it consistently avoids (e.g.
rhetorical questions, passive voice, exclamation points, first person, etc.).

SAMPLES:
{THIN}
{samples_text}
{THIN}

Respond with ONLY a raw JSON array of short rule strings, ordered by how
confident you are (most confident first). Each string should be a single,
specific, checkable rule, e.g. "Avoids rhetorical questions" or
"Uses second person ('you') throughout, never third person ('the user')".
No commentary, no markdown fences -- just the JSON array of strings.
"""


def build_mechanical_prompt(draft_text: str) -> str:
    return f"""You are doing a MECHANICAL proofreading pass only: grammar,
spelling, and punctuation errors. Do NOT suggest changes for flow, tone,
impact, structure, or style -- only genuine mechanical errors.

DRAFT:
{THIN}
{draft_text}
{THIN}

For the "source" field, use "Mechanical rule" plus the type of error, e.g.
"Mechanical rule: subject-verb agreement".
{JSON_FORMAT_INSTRUCTIONS}"""


def build_voice_prompt(draft_text: str, voice_label: str, voice_rules_text: str) -> str:
    return f"""You are checking a draft for BRAND VOICE conformance against
the rules below. Flag any sentence or passage that violates one of these
rules. Do not flag anything not covered by a specific rule below.

VOICE RULES ({voice_label}):
{THIN}
{voice_rules_text}
{THIN}

DRAFT:
{THIN}
{draft_text}
{THIN}

For the "source" field, cite the specific rule that justifies each flag,
e.g. "{voice_label}, rule 4" or, if it's an inferred rule, quote it, e.g.
"Inferred from sample pieces: avoids rhetorical questions".
{JSON_FORMAT_INSTRUCTIONS}"""


def build_readability_prompt(draft_text: str) -> str:
    return f"""You are doing a READABILITY pass. Flag: sentences that are
too long or complex, passive voice, and undefined jargon or acronyms.
For each flag, SUGGEST a fix but do not rewrite for creative impact -- keep
suggestions minimal and mechanical (e.g. splitting a sentence, converting
passive to active, defining a term on first use).

DRAFT:
{THIN}
{draft_text}
{THIN}

For the "source" field, use one of: "Readability: sentence length",
"Readability: passive voice", "Readability: undefined jargon".
{JSON_FORMAT_INSTRUCTIONS}"""


def build_seo_prompt(draft_text: str, keyword_text: str) -> str:
    return f"""You are doing an SEO pass against the keyword/SEO brief
below. Check keyword density (primary and secondary/semantic targets),
whether negative keywords appear, whether the header structure (H1/H2/etc.)
reflects the target keywords and search intent, and whether there are gaps
in an implied meta title/description. Flag missing semantic variants,
awkward keyword stuffing, structural gaps, and any negative keywords
present in the draft.

KEYWORD/SEO BRIEF:
{THIN}
{keyword_text}
{THIN}

DRAFT:
{THIN}
{draft_text}
{THIN}

For the "source" field, cite the specific part of the brief, e.g.
"SEO brief: secondary keyword 'workflow automation'" or
"SEO brief: search intent mismatch".
{JSON_FORMAT_INSTRUCTIONS}"""


# ---------------------------------------------------------------------------
# Step 0: Intake
# ---------------------------------------------------------------------------

def step0_intake():
    banner(f"{APP_NAME} -- Step 0: Intake")
    print("Let's gather what this run needs.\n")

    draft_path = ask_required_path("Path to the DRAFT you want polished")
    draft_text = read_file_text(draft_path)
    print(f"  Loaded draft: {draft_path} ({len(draft_text.split())} words)")

    print()
    print("A keyword/SEO doc is required (primary keyword, secondary/semantic")
    print("targets, negative keywords, search intent). This run can't continue")
    print("without it.")
    keyword_path = ask_required_path("Path to the KEYWORD/SEO doc")
    keyword_text = read_file_text(keyword_path)

    print()
    style_guide_path = ask_optional_path("Path to a STYLE GUIDE (Oxford comma, "
                                          "number formatting, capitalization, terms)")
    style_guide_text = read_file_text(style_guide_path) if style_guide_path else None

    print()
    print("Now, brand voice. This can come from a written brand voice doc, a")
    print("saved profile from a previous run, or -- if neither exists -- three")
    print("sample pieces the agent will infer rules from.")
    brand_name = ask("What brand/company is this draft for? (used to name the saved voice profile)", default="default")
    brand_slug = slugify(brand_name)

    voice_label, voice_rules_text = step0b_brand_voice(brand_slug)

    return {
        "draft_path": draft_path,
        "draft_text": draft_text,
        "keyword_text": keyword_text,
        "style_guide_text": style_guide_text,
        "brand_slug": brand_slug,
        "voice_label": voice_label,
        "voice_rules_text": voice_rules_text,
    }


def step0b_brand_voice(brand_slug: str):
    existing = load_profile(brand_slug)

    options = []
    if existing:
        options.append(f"Use the saved voice profile for '{brand_slug}' "
                        f"({len(existing['rules'])} rules, last updated {existing['last_updated']})")
    options.append("I have a written brand voice doc")
    options.append("No doc -- infer voice from 3 sample pieces")

    choice = choose("How should brand voice be determined for this run?", options)

    if existing and choice == 0:
        rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(existing["rules"]))
        return f"Saved voice profile ({brand_slug})", rules_text

    idx_doc = 1 if existing else 0
    idx_infer = 2 if existing else 1

    if choice == idx_doc:
        doc_path = ask_required_path("Path to the brand voice doc")
        doc_text = read_file_text(doc_path)
        return "Voice doc", doc_text

    # Infer from 3 samples
    return step0c_infer_voice(brand_slug)


def step0c_infer_voice(brand_slug: str):
    section("Inferring brand voice from 3 sample pieces")
    print("Provide 3 long-form pieces this company considers on-voice.")
    sample_paths = []
    sample_texts = []
    for i in range(1, 4):
        p = ask_required_path(f"Path to sample piece #{i}")
        sample_paths.append(str(p))
        sample_texts.append(f"--- SAMPLE {i} ({p.name}) ---\n{read_file_text(p)}")

    combined = "\n\n".join(sample_texts)
    prompt = build_voice_extraction_prompt(combined)
    print_prompt_block(prompt)

    while True:
        raw = read_multiline("Waiting for the AI's response (a JSON array of inferred rule strings)")
        data, err = extract_json_array(raw)
        if data is None or not all(isinstance(x, str) for x in data):
            print(f"  Couldn't parse that as a JSON array of strings ({err}). Please paste again.")
            continue
        rules = [r.strip() for r in data if r.strip()]
        break

    section("Inferred voice rules -- please review before they're applied")
    for i, r in enumerate(rules, 1):
        print(f"  {i}. {r}")

    print()
    print("You can now correct these rules: remove ones that are wrong, edit")
    print("wording, or add ones the AI missed.")
    rules = correct_rule_list(rules)

    save_profile(brand_slug, rules, sample_paths)
    print(f"\nSaved as the voice profile for '{brand_slug}'. Future runs for this "
          f"brand can reuse it instead of re-inferring.")

    rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
    return "Inferred rules (from sample pieces)", rules_text


def correct_rule_list(rules: List[str]) -> List[str]:
    while True:
        print()
        action = choose("What would you like to do?", [
            "These look good -- continue",
            "Remove a rule by number",
            "Edit a rule by number",
            "Add a new rule",
        ])
        if action == 0:
            return rules
        elif action == 1:
            n = ask("Which rule number to remove?")
            if n.isdigit() and 1 <= int(n) <= len(rules):
                removed = rules.pop(int(n) - 1)
                print(f"  Removed: {removed}")
            else:
                print("  Not a valid rule number.")
        elif action == 2:
            n = ask("Which rule number to edit?")
            if n.isdigit() and 1 <= int(n) <= len(rules):
                new_text = ask("New wording for this rule")
                rules[int(n) - 1] = new_text
            else:
                print("  Not a valid rule number.")
        elif action == 3:
            new_rule = ask("New rule to add")
            rules.append(new_rule)
        print()
        for i, r in enumerate(rules, 1):
            print(f"  {i}. {r}")


# ---------------------------------------------------------------------------
# Steps 1-4: the four analysis passes
# ---------------------------------------------------------------------------

def run_pass(category: str, prompt_text: str, next_id: int) -> List[Flag]:
    section(CATEGORY_TITLES[category])
    print_prompt_block(prompt_text)
    flags = collect_flags_from_paste(category, next_id)
    print(f"  Recorded {len(flags)} flag(s) for this pass.")
    return flags


def run_all_passes(context: dict) -> List[Flag]:
    all_flags: List[Flag] = []
    next_id = 1

    mech_prompt = build_mechanical_prompt(context["draft_text"])
    flags = run_pass("mechanical", mech_prompt, next_id)
    all_flags.extend(flags)
    next_id += len(flags)

    voice_prompt = build_voice_prompt(context["draft_text"], context["voice_label"], context["voice_rules_text"])
    flags = run_pass("voice", voice_prompt, next_id)
    all_flags.extend(flags)
    next_id += len(flags)

    read_prompt = build_readability_prompt(context["draft_text"])
    flags = run_pass("readability", read_prompt, next_id)
    all_flags.extend(flags)
    next_id += len(flags)

    seo_prompt = build_seo_prompt(context["draft_text"], context["keyword_text"])
    flags = run_pass("seo", seo_prompt, next_id)
    all_flags.extend(flags)
    next_id += len(flags)

    if context.get("style_guide_text"):
        print()
        print("Note: a style guide was supplied. If any flags above conflict with")
        print("it, the style guide should win -- review those cases when you get")
        print("to the approval step.")

    return all_flags


# ---------------------------------------------------------------------------
# Step 5: Review / approve / reject
# ---------------------------------------------------------------------------

def print_flag(f: Flag) -> None:
    print(f"  [{f.id}] ({f.label()}) \"{f.original}\" -> \"{f.suggestion}\"")
    print(f"       source: {f.source}")
    if f.reason:
        print(f"       reason: {f.reason}")


def review_all_at_once(flags: List[Flag]) -> None:
    section("Reviewing all flags at once")
    for f in flags:
        print_flag(f)
    print()
    print("Reply with the numbers to ACCEPT, e.g. \"1,3,5\" or \"all\" or \"none\".")
    answer = ask("Which flags do you accept?")
    accept_ids = parse_id_list(answer, [f.id for f in flags])
    for f in flags:
        f.decision = "accept" if f.id in accept_ids else "reject"


def review_pass_by_pass(flags: List[Flag]) -> None:
    section("Reviewing pass by pass")
    for category in CATEGORY_ORDER:
        group = [f for f in flags if f.category == category]
        if not group:
            continue
        print()
        print(f"-- {CATEGORY_TITLES[category]} --")
        for f in group:
            print_flag(f)
        answer = ask(f"Which of these do you accept? (numbers, \"all\", or \"none\")")
        accept_ids = parse_id_list(answer, [f.id for f in group])
        for f in group:
            f.decision = "accept" if f.id in accept_ids else "reject"


def review_written_markup(flags: List[Flag], draft_text: str) -> List[Flag]:
    section("Written markup review")
    review_file = Path("editing_assistant_review.md")
    lines = ["# Editing Assistant -- Review Draft",
             "",
             "For each item below, replace `[ ]` with `[x]` to ACCEPT that change,",
             "or leave it as `[ ]` to reject it. Save this file when done.",
             "",
             "## Flags", ""]
    for f in flags:
        lines.append(f"- [ ] **#{f.id} ({f.label()})** \"{f.original}\" -> \"{f.suggestion}\"  ")
        lines.append(f"  source: {f.source}" + (f" -- {f.reason}" if f.reason else ""))
        lines.append("")
    lines.append("## Original draft (for reference)")
    lines.append("")
    lines.append(draft_text)
    review_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"Wrote {review_file} -- open it, check the boxes for changes you accept,")
    print("save it, then come back here.")
    input("Press Enter once you've saved the file... ")

    saved = read_file_text(review_file)
    checked_ids = set(int(m) for m in re.findall(r"- \[[xX]\] \*\*#(\d+)", saved))
    for f in flags:
        f.decision = "accept" if f.id in checked_ids else "reject"
    return flags


def parse_id_list(answer: str, universe: List[int]) -> set:
    a = answer.strip().lower()
    if a in ("all", "accept all"):
        return set(universe)
    if a in ("none", "reject all", ""):
        return set()
    ids = set()
    for part in re.split(r"[,\s]+", a):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids & set(universe)


def step5_review(flags: List[Flag], draft_text: str) -> List[Flag]:
    banner(f"{APP_NAME} -- Step 5: Diff / Review")
    if not flags:
        print("No flags were recorded across any pass -- nothing to review.")
        return flags

    mode = choose("How would you like to review the flags?", [
        "All at once (one list, accept/reject by number)",
        "Pass by pass (mechanical, then voice, then readability, then SEO)",
        "Written markup (a checklist file you edit and save)",
    ])
    if mode == 0:
        review_all_at_once(flags)
    elif mode == 1:
        review_pass_by_pass(flags)
    else:
        review_written_markup(flags, draft_text)
    return flags


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_decision_log(flags: List[Flag], context: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Editing Assistant -- Decision Log", "", f"Generated: {ts}",
             f"Draft: {context['draft_path']}", ""]
    accepted = [f for f in flags if f.decision == "accept"]
    rejected = [f for f in flags if f.decision == "reject"]
    lines.append(f"Total flags: {len(flags)}  |  Accepted: {len(accepted)}  |  Rejected: {len(rejected)}")
    lines.append("")
    for category in CATEGORY_ORDER:
        group = [f for f in flags if f.category == category]
        if not group:
            continue
        lines.append(f"## {CATEGORY_TITLES[category]}")
        lines.append("")
        for f in group:
            status = "ACCEPTED" if f.decision == "accept" else "rejected"
            lines.append(f"- **[{status}]** \"{f.original}\" -> \"{f.suggestion}\"")
            lines.append(f"  - source: {f.source}")
            if f.reason:
                lines.append(f"  - reason: {f.reason}")
        lines.append("")

    claims_note = (
        "## Claims & statistics\n\n"
        "This tool does not verify factual claims or statistics -- if the draft "
        "contains any, route them to a separate fact-check step before publishing.\n"
    )
    lines.append(claims_note)
    return "\n".join(lines)


def apply_accepted_changes(draft_text: str, flags: List[Flag]) -> (str, List[Flag]):
    """
    Best-effort string replacement of accepted changes. Returns the updated
    text and a list of flags whose 'original' text could not be found
    verbatim (so the user knows to apply those by hand).
    """
    updated = draft_text
    not_found = []
    for f in flags:
        if f.decision != "accept":
            continue
        if not f.original:
            not_found.append(f)
            continue
        if f.original in updated:
            updated = updated.replace(f.original, f.suggestion, 1)
        else:
            not_found.append(f)
    return updated, not_found


def step_final_output(flags: List[Flag], context: dict) -> None:
    banner(f"{APP_NAME} -- Final Output")
    log_text = build_decision_log(flags, context)
    log_path = Path("editing_assistant_decision_log.md")
    log_path.write_text(log_text, encoding="utf-8")
    print(f"Wrote the decision log to: {log_path}")

    want_draft = confirm("Do you also want an assembled clean draft with the "
                          "accepted changes applied?", default=True)
    if want_draft:
        updated_text, not_found = apply_accepted_changes(context["draft_text"], flags)
        draft_out_path = Path(f"editing_assistant_polished_{context['draft_path'].stem}{context['draft_path'].suffix or '.txt'}")
        draft_out_path.write_text(updated_text, encoding="utf-8")
        print(f"Wrote the polished draft to: {draft_out_path}")
        if not_found:
            print()
            print(f"Note: {len(not_found)} accepted change(s) couldn't be auto-applied "
                  "because the exact original text wasn't found verbatim in the draft "
                  "(this can happen if earlier accepted edits changed the surrounding "
                  "text). Apply these by hand -- they're listed in the decision log:")
            for f in not_found:
                print(f"  - #{f.id}: \"{f.original}\" -> \"{f.suggestion}\"")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    banner(APP_NAME)
    print("A step-by-step content polish workflow you run alongside any AI chat")
    print("tool. Nothing is auto-applied -- every change is cited to a source")
    print("and you approve or reject it individually.")

    try:
        context = step0_intake()
        flags = run_all_passes(context)
        flags = step5_review(flags, context["draft_text"])
        step_final_output(flags, context)
    except KeyboardInterrupt:
        print("\n\nStopped early. Nothing further was written.")
        sys.exit(1)

    banner("Done")
    print("Thanks for using the Editing Assistant.")


if __name__ == "__main__":
    main()
