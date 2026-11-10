# debrief

Review your coding-agent sessions: where you had to correct the agent, what it missed, and what
to change so it happens less.

debrief reads the session logs Claude Code and Codex already keep on your machine, labels each
of your turns, groups the labels into findings with evidence, and builds a local report site.

## Install

    git clone <repo> && cd debrief
    uv tool install .

Needs Python 3.13 and a logged-in `claude` CLI (the default labeler runs through `claude -p` on
your Claude subscription).

## Use

    debrief sync --dry-run     # ingest logs, then say how many model calls labeling would take
    debrief sync               # ingest, label, find patterns, build the report
    debrief open               # serve the report at http://127.0.0.1:8765 and open it

Useful flags for `sync`:

    --project DIR        only sessions run in this directory or below it
    --since YYYY-MM-DD   only logs and evidence from this date on
    --import DIR         also load a folder of JSONL logs (Claude Code, Codex or Pi format)
    --include-headless   also load scripted runs (see below)
    --llm openrouter     label with Jev through OpenRouter instead of Claude

A re-run only reads logs that changed and only labels turns that are new.

`debrief <stage> --help` runs one stage on its own: `ingest`, `label`, `synthesize`,
`recommend`, `verify`, `export`, `prune`.

## What gets loaded

- Claude Code logs in `~/.claude/projects` and Codex logs in `~/.codex/sessions`.
- Scripted runs are skipped by default: `claude -p`, `codex exec`, and sessions started through
  an SDK or a wrapper app that have fewer than two turns from a person. On the author's machine
  that was 1,038 of 1,274 logs.
- Sub-agent logs are skipped, so tool calls made inside a sub-agent are not counted.
- A resumed or forked session copies its history into the new log. Each turn is loaded once.

## Where your data goes

- Everything debrief writes is under `~/.local/share/debrief` (set `DEBRIEF_HOME` to move it).
- Labeling and synthesis send transcript text to a model: by default Claude, through your own
  `claude` CLI. With `--llm openrouter` it goes to OpenRouter and needs `OPENROUTER_API_KEY`.
- The report holds your transcripts. `debrief open` serves it on 127.0.0.1 only. Token-shaped
  strings (GitHub, Anthropic, OpenAI, AWS, Slack, Stripe, private keys and others) are replaced
  with `[redacted]` in the report, but the redaction is pattern-based: read before you share.

## Which labeler

| | Claude Haiku (default) | Jev (`--llm openrouter`) |
|---|---|---|
| Needs | a Claude subscription | an OpenRouter key |
| Sends transcripts to | Anthropic, via your CLI | OpenRouter |
| Confidence | the model's own estimate | token probabilities |

On 66 turns where the two disagreed, a blind reader sided with Jev 32 times and Haiku 17 on the
turn-level questions. Haiku misses more one-line asides in long turns. There is no human-labeled
set behind these numbers.

## Limits in this version

- GitHub PR, CI and review data load only for sessions fetched from Entire (`debrief entire`,
  `debrief github --repo owner/name`). Local sessions get no CI or review pages.
- Labels vary between runs: on a set of hard cases Haiku changed its answer on about 15% of
  rows between two identical runs.
- Pi logs are parsed but only tested against a made-up log.

## Develop

    uv sync
    uv run pytest
    uv run ruff check .
