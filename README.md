# debrief

Review your coding-agent sessions: where you had to correct the agent, what it missed, and what
to change so it happens less.

debrief reads the session logs Claude Code and Codex already keep on your machine, labels each
of your turns, groups the labels into findings with evidence, and builds a local report site.

## Install

With Homebrew on a Mac:

    brew install mvwi/tap/debrief

With uv or pipx, on Python 3.13 or newer:

    uv tool install debrief-cli
    pipx install debrief-cli

The package on PyPI is `debrief-cli`; the command it installs is `debrief`. The PyPI package
named `debrief` is a different project, so `uvx debrief` and `pip install debrief` fetch the
wrong tool. To run debrief once without installing it:

    uvx --from debrief-cli debrief open

From source:

    git clone https://github.com/mvwi/debrief && cd debrief
    uv tool install .

The default labeler runs through `claude -p` on your Claude subscription, so it needs a
logged-in `claude` CLI. Without one, pick OpenRouter and save a key on the Settings page of
`debrief open`. `gh` is optional: with it, `debrief sync` adds pull-request data.

## Use

    debrief sync --dry-run     # ingest logs, then say how many model calls labeling would take
    debrief sync               # ingest, label, find patterns, build the report
    debrief open               # serve the report at http://127.0.0.1:8765 and open it

Useful flags for `sync`:

    --project DIR        add this project to the ones sync analyses
    --since YYYY-MM-DD   only logs and evidence from this date on
    --import DIR         also load a folder of JSONL logs (Claude Code, Codex or Pi format)
    --include-headless   also load scripted runs (see below)
    --llm openrouter     label and synthesize through OpenRouter, whatever config.toml says

A re-run only reads logs that changed and only labels turns that are new.

You pick which projects get analysed. A project is the folder a session ran in. The first
`debrief sync` reads your logs, lists the projects it found with their size, and stops:

    debrief sync --project ~/Dev/myrepo    # pick it (kept in config.toml), then analyse

Only picked projects are labeled, analysed and shown. Each gets its own findings and ideas,
and an idea names the project it lands in (`myrepo: CLAUDE.md`). The picker in the report's
side bar scopes every page to one project. Findings about how you prompt come from the picked
projects together. To drop a project:

    debrief config --drop ~/Dev/myrepo     # its findings stay in the database, hidden

Unticking it on the Projects page does the same.

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
- `debrief sync` asks GitHub about the pull requests your sessions produced, through your own
  logged-in `gh` CLI and read-only. It sends repo names, PR numbers, branch names and commit
  SHAs, and nothing from your transcripts. `--no-github`, or `[github] enabled = false` in
  config.toml, turns it off; without `gh` it is skipped.
- The report holds your transcripts. `debrief open` serves it on 127.0.0.1 only.
- Secrets are replaced with `[redacted]` in the report and in anything debrief uploads. The text
  sent to the model for labeling and synthesis is not redacted. Three things are caught: known
  token shapes (GitHub, Anthropic, OpenAI, AWS key ids, Slack, Stripe, Google, Resend and other
  vendor prefixes, JWTs, private keys, bearer tokens, cookies, passwords in URLs); values of 12 or
  more characters given to a name that sounds secret (`API_KEY=`, `"client_secret":`,
  `--token`); and the exact values of your saved OpenRouter key and of environment variables
  whose names contain KEY, TOKEN, SECRET, PASSWORD or CREDENTIAL. It is pattern matching, not a
  guarantee. It misses a secret with no known shape and no telling name beside it: a password
  in a sentence, a token in a URL parameter called `t`, a short password. It does not remove
  email addresses, IP addresses, host names or people's names. Read before you share.

## Which labeler

| | Claude Haiku (default) | Jev (`--llm openrouter`) |
|---|---|---|
| Needs | a Claude subscription | an OpenRouter key |
| Sends transcripts to | Anthropic, via your CLI | OpenRouter |
| Confidence | the model's own estimate | token probabilities |
| 2,774 items (236 sessions) | 33 min, no extra charge | about 3 min, about $0.37 |

The Jev time and cost are scaled up from a 74-item run (4 s, $0.0098).

To pick the model for each stage, write `~/.local/share/debrief/config.toml`:

    [label]
    llm = "openrouter"            # Jev: faster and closer to a careful reader, needs a key
    [synthesize]
    model = "claude-sonnet-5"     # optional; each llm has a default

Stages are `label`, `synthesize`, `recommend` and `verify`. `debrief config` prints what each
will use. A flag beats an environment variable, which beats the file.

### Settings page

`debrief open` has a Settings page that does the same without editing files. It picks the
provider and model for each stage, turns the GitHub lookups on or off, and saves an OpenRouter
key.

Saving needs the link `debrief open` prints, which carries a token for that run: someone else
on the same machine can reach the port but cannot change your settings. They can still read the
report, as they can read the files.

- Saving rewrites `config.toml`; comments you wrote in it are lost.
- The key goes in `~/.local/share/debrief/.env`, readable only by you. The page shows its last
  four characters and never the key. `OPENROUTER_API_KEY` set in your shell wins over the saved one.
- The server takes these writes only from a page it served itself, on `127.0.0.1`.

### Run from the page

`debrief open` can also run the analysis, so you need not go back to the terminal. With no
report yet it reads your local logs first (no model calls) and serves what it found.

The Projects page is where you do it: tick the projects to analyse (each tick is saved to
`config.toml` at once), ask for an estimate, then press "Check my data now". It shows the stage
and the count while the run goes, and has a Cancel button. When the run ends every page shows
the new report without a refresh. Underneath it uses:

    GET    /api/run        state (idle, running, done, failed, cancelled), stage, done/total,
                           start and end times, exit code, last 30 lines of output
    POST   /api/run        start `debrief sync`; 409 if a run or an estimate is going
    DELETE /api/run        stop the run and the `claude -p` calls it started
    POST   /api/estimate   run `debrief sync --dry-run`, rebuild the pages, return the counts

- The writes need the same token as the Settings page. Nothing in a request reaches the command
  line: what to analyse comes from `config.toml`.
- One run at a time. Its output is in `~/.local/share/debrief/run.log`, replaced on each run,
  with the OpenRouter key blanked.
- Ctrl-C stops a run too. If the server is killed outright, the next `debrief open` or the
  next start stops the run it left behind.
- A stopped run loses little: labels and bundles are kept, so the next run goes on from there.

How they compare, measured on the prototype's data with no human-labeled set behind it:

- On 66 turns where the two disagreed, a blind reader sided with Jev 32 times and Haiku 17 on
  the turn-level questions. Three definitions were then tightened.
- After that, on 155 ordinary turns, Haiku and Jev agree on 93% for "agent named a problem and
  did not file it" (kappa 0.77) and 92% for "the agent could have found this itself" (kappa
  0.66), where Haiku says yes about 1.7 times as often as Jev, mostly at low confidence.

## Limits in this version

- A local session is linked to a pull request only when its log shows one: the URL that
  `gh pr create` printed, or a branch it pushed that is the head of a PR. On the author's
  machine that linked 33 of the 53 sessions that pushed. Of the 20 left out, 12 pushed
  straight to main or another trunk branch.
- A session that pushes straight to main gets no CI or review data, even when CI runs on
  those commits. If you work alone and never open pull requests, the report will show no
  CI for you. The same goes for a PR opened by hand from a branch pushed outside the session.
- A CI failure counts for a session when its commit was made while the session ran (give or
  take ten minutes). Review comments count for every session linked to the PR.
- Sessions fetched from Entire still need `debrief github --repo owner/name`; `sync` does not
  fetch their GitHub data.
- A session's project is the folder it ran in, or the project of the repo it opened a PR in.
  A session run in a git worktree joins its repo's project when ingest saw the worktree
  while it still existed, or when the log names the repo, as Codex logs do. A Claude Code
  session in a worktree that was deleted before the first ingest, and that opened no PR,
  shows up as a project of its own, named after the worktree folder.
- A picked project that has not been synced yet shows in the report with no labels.
- "How it works" describes the whole run; the project picker does not scope its numbers.
- Labels vary between runs: on a set of hard cases Haiku changed its answer on about 15% of
  rows between two identical runs.
- Pi logs are parsed but only tested against a made-up log.

## Develop

    uv sync
    uv run pytest
    uv run ruff check .
