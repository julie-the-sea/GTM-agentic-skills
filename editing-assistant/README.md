# Editing Assistant

A step-by-step content polish workflow that runs as a single Python script.
It works with **any AI chat platform** you already use (ChatGPT, Claude.ai,
Gemini, Copilot, etc.) — it doesn't call any AI API itself, needs no API
key, no billing account, and no extra installs. That makes it a good fit
for teams that don't have budget for a dedicated API integration but do
have an AI chat subscription open in a browser tab.

## What it does

It runs your draft through four review passes, in order, and never
silently changes anything:

1. **Mechanical pass** — grammar, spelling, punctuation only. No rewriting
   for flow or impact.
2. **Brand voice conformance** — checked against either a written brand
   voice doc, a previously saved voice profile, or rules inferred from 3
   sample pieces you consider on-voice (with a chance to correct the
   inferred rules before they're applied).
3. **Readability pass** — sentence length, passive voice, undefined
   jargon. Suggests fixes, doesn't auto-rewrite.
4. **Keyword/SEO pass** — checked against a keyword/SEO brief you supply:
   density, missing semantic variants, header structure, and meta
   title/description gaps.

Every flag from every pass cites the specific source that justifies it
(e.g. "Voice doc, rule 4" or "Inferred from sample pieces: avoids
rhetorical questions"). At the end, you review every flag and approve or
reject it individually — nothing is applied automatically.

**Out of scope, always:** it does not generate sentences, arguments,
hooks, or structural rewrites. It does not verify factual claims or
statistics — those get flagged in the output as needing a separate
fact-check pass.

## How it works (the copy/paste loop)

Judging whether a sentence violates your brand voice, or whether a
paragraph satisfies search intent, takes actual reasoning — that's not
something plain code can decide on its own. So at each step, the script:

1. Prints a ready-made prompt with your draft and the relevant reference
   material already embedded in it.
2. You copy that prompt and paste it into whatever AI chat you already
   use.
3. You paste the AI's reply back into the terminal and type `END` on its
   own line.
4. The script parses the reply and moves you to the next step.

This means there's a fair amount of copy-pasting, but it also means the
tool works identically no matter which AI product your team has access
to, and costs nothing beyond whatever you're already paying for that
chat subscription.

## Requirements

- Python 3.8 or later. No third-party packages — standard library only.
- Any AI chat tool (used outside the script, in a separate window/tab).

## Running it

```bash
python3 editing_assistant.py
```

Follow the prompts. You'll be asked for:

- The path to your draft file (plain text).
- The path to your keyword/SEO doc (**required** — the run stops if you
  don't have one, since Step 4 can't run without it).
- Optionally, a style guide (Oxford comma, number formatting,
  capitalization, industry terms). If you supply one and it conflicts
  with a brand-voice flag, the style guide should win — you'll see both
  at review time and can decide.
- Brand voice: a written doc, a previously saved profile for this brand,
  or (if neither exists) three sample file paths to infer rules from.

At each of the four passes, copy the printed prompt into your AI chat,
paste the reply back, and the script records the flags.

### Brand voice profiles

If you go the "infer from 3 samples" route, the script shows you the
rules it extracted and lets you remove, edit, or add rules before saving.
Once saved, it's written to a file named
`editing_assistant_voice_profile_<brand>.json` in the folder you ran the
script from. The next time you run the script for the same brand name,
it offers to reuse that saved profile instead of asking for samples
again — so the correction work you do only has to happen once.

### Reviewing flags (Step 5)

You'll be asked how you want to go through the flags:

- **All at once** — one list, you type which numbers to accept (e.g.
  `1,3,5`, or `all`, or `none`).
- **Pass by pass** — mechanical flags first, then voice, then
  readability, then SEO, approving each group before moving to the next.
- **Written markup** — the script writes a checklist file
  (`editing_assistant_review.md`) with a checkbox per flag; you open it,
  check the ones you accept, save, and come back to the terminal.

### Output files

- `editing_assistant_decision_log.md` — always written. A line-by-line
  record of every flag, its source citation, and whether it was accepted
  or rejected.
- `editing_assistant_polished_<draft filename>` — written only if you say
  yes when asked. A copy of your draft with the accepted changes applied.
  If an accepted change's original text can't be found verbatim (this can
  happen when an earlier accepted edit changes the surrounding text), it's
  listed at the end so you can apply it by hand — it's also in the
  decision log.

## Tips

- Keep your draft, keyword doc, and any style guide/voice doc as plain
  text files (`.txt` or `.md`) for reliable reading.
- When pasting an AI's reply, paste the raw JSON array only. The script
  can usually recover if the AI added a code fence or a sentence of
  commentary, but the cleaner the paste, the fewer retries you'll need.
- If a pass produces a reply that won't parse, you can type `SKIP` to
  record zero flags for that pass rather than getting stuck.
- Run the script again for a second brand by giving a different brand
  name at the intake step — profiles are kept separate per brand.
