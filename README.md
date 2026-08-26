# verify-citations

AI is genuinely good at research synthesis — it can read fast and summarize faster. What it's bad at is telling you when it's wrong: a hallucinated statistic gets stated with exactly the same confident tone as a real one, and a citation sitting next to a claim isn't proof the claim is true. It might be the right source paraphrased sloppily, the wrong source entirely, or a source that never actually gets checked.

`verify_citations.py` does what a careful editor would do before a piece runs: it takes every claim and citation in a document, goes back to the **actual source**, and checks whether that source really says what's being claimed. Anything it can't confirm gets flagged instead of quietly passed through.

It works with whatever AI backend you have access to — Claude, OpenAI, or any OpenAI-compatible API (including local models) — see [Providers](#providers) below.

## What it does

1. **Extracts** every checkable claim from your document — statistics, dates, quotes, specific factual assertions — along with whatever citation was attached to each one.
2. **Fetches** the real source for each citation: pulls the actual web page, or reads the actual local file you point it at.
3. **Classifies** each claim by comparing it against the real source text, strictly:

   | Verdict | Meaning |
   |---|---|
   | ✓ **Confirmed** | The source actually states this specific fact/figure/quote. |
   | ✗ **Contradicted** | The source is reachable and on-topic, but says something different. |
   | ⚠ **Unconfirmed** | The source was read, but doesn't contain support for this specific claim. |
   | ? **Unreachable** | The source couldn't be fetched or found at all. |
   | ✎ **Uncited** | No source was given, or the attribution was too vague to trace. |

4. **Reports**: a markdown file with a summary count, a claim-by-claim table (with the actual evidence quoted for every row, including confirmed ones), and an annotated copy of your original document with an inline marker after every claim.

Only "Confirmed" means it checks out. Everything else is a flag worth your attention — the whole point of this tool is to surface those instead of smoothing them over.

## Install

```bash
pip install requests beautifulsoup4 lxml anthropic openai pdfplumber
```

You only need the SDK for the provider you're actually using (`anthropic` or `openai`), and `pdfplumber` only if a document or source is a `.pdf`. The rest (`requests`, `beautifulsoup4`, `lxml`) are always required.

## Quick start

```bash
export ANTHROPIC_API_KEY=sk-ant-...

python verify_citations.py my_report.md \
    --sources acme_market_report.pdf \
    --output verification_report.md
```

- `my_report.md` is the document you want checked.
- `--sources` lists any local files your document cites by name (a PDF report, an internal doc) rather than by URL — pass as many as you need. If a citation is a URL, it's fetched automatically; you don't need to list it.
- `--output` is where the report gets written. Leave it off and the report prints to stdout instead.

The script prints its progress (extracting, fetching, classifying) to stderr as it goes, so you can see what it's doing on a long document.

## Providers

Pass `--provider` to choose which AI does the actual judgment calls:

| Provider | Flag | Notes |
|---|---|---|
| Anthropic (Claude) | `--provider anthropic` (default) | Needs `ANTHROPIC_API_KEY` (or `--api-key`). |
| OpenAI | `--provider openai` | Needs `OPENAI_API_KEY` (or `--api-key`). |
| Anything OpenAI-compatible | `--provider openai --base-url <endpoint>` | Azure OpenAI, OpenRouter, Groq, Together AI, or a local server — Ollama (`http://localhost:11434/v1`), LM Studio, vLLM. |

Override the model with `--model` (e.g. `--model gpt-4o` or `--model claude-opus-4-...`).

Want to use something that isn't OpenAI-compatible and isn't Anthropic (Gemini, Cohere, an in-house model)? Subclass `LLMProvider` near the top of `verify_citations.py` and implement one method:

```python
class MyProvider(LLMProvider):
    def complete(self, system: str, user: str) -> str:
        ...  # call your API
        return the_reply_as_plain_text
```

Everything else in the script keeps working unmodified — it only ever calls `provider.complete(system, user)`.

## How citations get resolved

- **A URL in the citation** → fetched directly. If the page won't load (dead link, paywall, blocked), the claim is marked **Unreachable**, not silently skipped.
- **A named document with no URL** ("Acme Market Intelligence Report, Section 3.2") → matched against the files you passed via `--sources`. With one file passed, it's used directly. With several, the script scores each by how well the citation's wording overlaps with that file's name and content, and picks the best match.
- **No citation, or a vague one** ("industry analysts say...") → marked **Uncited**. This tool doesn't do open-ended web search to go find a source you didn't provide — if a named report isn't one of your `--sources` files and isn't a URL, it's marked Unreachable with an explanation, rather than guessed at.

## Limitations, honestly

- The matching between a named citation and one of your `--sources` files is a heuristic (keyword overlap), not certain — for anything but a single obvious source file, spot-check that it grabbed the right one.
- Long sources get truncated (15,000 characters by default, adjustable with `--max-source-chars`) before being sent to the model, so a claim's supporting evidence buried very deep in a huge document could be missed. Increase the limit for big files if your provider's context window allows it.
- This isn't a fully deterministic tool — the classification step is a judgment call made by an LLM, and while the prompts are written to be strict and skeptical, it can still make mistakes, just like the research pass it's checking. Treat its flags as a strong starting point for a human review, not a guarantee.
- No built-in web search for a named report that isn't a URL and isn't one of your `--sources` files — pass the file or find the URL yourself.

## License

MIT — use it, adapt it, ship it.
