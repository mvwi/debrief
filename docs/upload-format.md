# Upload format, version 1

What `debrief push` would send to a team server. No server exists yet: `debrief push --dry-run`
writes the same files to a folder and sends nothing. `debrief/push.py` builds them.

The server stores and shows; it runs no models. Labels, findings and ideas are made on each
person's machine before the upload.

## What one push holds

A push is a set of files. A dry run writes them to a folder as they are named here.

    manifest.json                always sent
    sessions/<name>.json         one per session of a picked project
    findings.json                findings, ideas and the evidence they cite
    github.json                  pull requests, CI failures and review comments

Every file but the manifest is an *object*. An object is sent only when it changed (see
"Later pushes"). Objects are UTF-8 JSON with sorted keys and no spaces.

Only projects the user picked (`[projects] include` in config.toml) contribute objects. The
others are a count in the manifest: no name, no path, no sessions.

Only the pusher's own sessions are sent. A session that names an author was fetched from
Entire and may be a teammate's, and a server files every row under the signed-in user, so it
is left out with the findings and ideas that cite it. Sessions read from local logs name no
author and are taken as the pusher's.

### manifest.json

    {
     "format": 1,
     "tool": "debrief 0.1.1",
     "machine": "0b9c4c0e-3f0a-4d0e-9d55-6a1f4b6f1f0a",
     "redaction": 3,
     "projects": [
      {"key": "octo/alpha", "name": "alpha", "sessions": 12, "turns": 140, "analysed_at": "2026-01-05T11:00:00Z"},
      {"key": "local:5f2a90c1d3e4", "name": "beta", "sessions": 3, "turns": 20, "analysed_at": null}
     ],
     "unpicked": 4,
     "objects": {
      "sessions/a1.json": {"sha256": "9f2c…", "bytes": 27415},
      "findings.json": {"sha256": "41aa…", "bytes": 18877}
     },
     "sent": ["sessions/a1.json"]
    }

- `format`: an integer. A server refuses a number it does not know. Adding a field does not
  change it; removing one, or changing what one means, does.
- `tool`: the debrief version that built the push. `findings.json` names levers
  (`agent_config`, `test_harness`, ...) whose labels belong to that version and are not sent.
- `machine`: see "Who and where".
- `redaction`: the version of the redaction rules every object in `objects` was scrubbed under,
  an integer that grows when the rules change. A server can keep it beside each object it
  stores. It is 0 until the redaction module carries a version.
- `projects`: the picked projects that have sessions. `sessions` and `turns` count what is sent;
  `turns` are the person's own turns.
- `unpicked`: how many other projects debrief found on the machine.
- `objects`: every object that should exist for this machine once the push is done, sent or
  not, with the SHA-256 and length of its bytes.
- `sent`: the objects that travel with this manifest.

With no project picked, `projects` is `[]`, `objects` is `{}` and nothing else is sent.

### sessions/<name>.json

`<name>` is the session id. An id that is not a plain file name (it must match
`[A-Za-z0-9_-][\w.-]*`) is replaced by the first 32 hex digits of its SHA-256; the object's
`id` field always holds the real id.

The object is the session's row from the local report plus its transcript:

- `id`, `name`, `agent`, `model`, `started`, `last`, `prompt` (first 400 characters),
  `author` (always null: see "Who and where").
- `project`: a project `key` from the manifest.
- Counts: `human_turns`, `turn_kinds`, `corrections`, `reversals`, `defects`, `could_find`,
  `not_filed`, `relayed`, `tool_calls`, `tool_errors`, `checkpoints`, `ci_failures`.
  `friction` lists `[msg_idx, [signal, ...]]` for each flagged turn.
- Session labels: `outcome`, `task_type`, `prompt_style`, `specificity`, `planned_first`,
  `tests_added`.
- `prs`: `[{"n": "owner/name#7", "state": "merged"}]`; `settled` is true when one is merged or closed.
- `messages`, in order: `i` (index in the session), `r` (`user` or `assistant`), `ts`, `t` (text,
  cut at 8,000 characters), `k` for user messages (`human`, or `injected` for skill, command
  and hook text). A labeled turn also has `l`, `{field: [value, confidence]}` without the
  `none` and `false` values, and `sig`, its signals.
- `c` on a message: its tool calls, each `n` (tool name), `in` (the command, path or query, cut
  at 300 characters), `err` (the last 800 characters of output, failed calls only) and `w` (files
  written).
- `checkpoints`: `sha`, `msg`, `ts`.

A turn is named by `(session id, i)` everywhere, as it is in the local database.

The counts and `sig` are worked out on the client with a confidence floor of 0.7. A server
that wants another floor has the raw confidence of every label in `l` and can count again.

### findings.json

    {"run_id": "20260105T110000Z", "findings": {"1": {...}}, "recommendations": [...], "refs": {...}}

The latest analysis run, cut down to the picked projects. Absent when there are no findings.

- A finding: `id`, `kind`, `title`, `detail`, `fix`, `project` (a key, or null when it is about
  all picked projects together), `evidence` (ref ids).
- A recommendation (the report calls it an idea): `id`, `priority`, `lever`, `title`,
  `project`, `problem`, `change`, `target`, `effort`, `impact`, `findings` (finding ids),
  `evidence` (the refs a checker found to support it, or all of them when `checked` is false),
  `evidence_all`, `checked`, `weak`.
- `refs`: ref id to what it points at. `m:<session id>:<i>` is a turn, with an excerpt and the
  agent text before it. `e:check_failure:<id>` is a CI failure with the tail of its log.
  `e:review_comment:<id>` is a review comment with an excerpt of its body.

The upload is stricter than the local report, which keeps a finding when some of its evidence
is in a picked project and an idea when some of its findings are kept. A model wrote each text
from all of its sources, so here it is all or nothing:

- a finding is sent only when every piece of evidence stored for it is in the bundle: a turn of
  a session that is sent, or a CI or review event of a picked project's repo. Evidence that
  belongs to no known project counts as outside;
- a recommendation is sent only when every finding it was built from is sent;
- both must be about a picked project, or about all picked projects together (`project` null).

A `project` that is not null always names a key in the manifest. The dry run says how many
findings and ideas the report shows that the upload leaves out.

Finding and recommendation ids are counters in one machine's database. They are unique only
inside one `findings.json`: store them under (user, machine).

### github.json

Rows for the pull requests the bundled sessions worked on, and no others. Absent without any.

- `prs`: `"owner/name#7"` to `title`, `state`, `url`.
- `ci.failures`: failed check runs on merged or closed PRs: `id`, `name`, `at`, `class`,
  `flaky`, `pr`, and the packages, tests and files the log names. `ci.runs_by_pr` counts all
  completed runs per PR, for failure rates.
- `reviews`: top-level review comments on merged or closed PRs: `id`, `author`, `bot`, `kind`,
  `pr`, `path`, `resolved`, `outdated`, `type`, `held`. No comment text; a cited comment's
  text is in `findings.json`.

## Who and where

- **User.** Not in the bundle. Local logs name no author, and a name the client claims proves
  nothing. The server takes the user from the login that carries the push.
- **Machine.** `machine` is a random UUID, made the first time `push` runs and kept in
  `DEBRIEF_HOME/machine_id`. It is not the hostname. One user may push from several machines;
  a manifest speaks only for its own (user, machine).
- **Project.** `key` is the GitHub `owner/name` when the project's sessions are tied to a repo
  (by a pull request, or by a remote the log records). That is what lets a server put two
  people's work on one repo side by side, and it is the only place a repo name is sent besides
  PR ids. A project with no known repo gets `local:` plus 12 hex digits of
  SHA-256(`machine:folder path`): stable on one machine, meaningless off it. `name` is the last
  part of the folder path. The folder path itself is never a field.
- **Session.** The id the agent gave it, already unique.

## Later pushes

The manifest is the whole truth for one (user, machine). The objects sent are only the difference.

1. The client builds every object and hashes its bytes.
2. It sends an object when the server does not hold that path with that hash.
3. The manifest lists every object either way.
4. The server keeps an object the manifest lists, replaces one that arrives, and deletes any
   object of this (user, machine) the manifest no longer lists.

Step 4 is how removal works: unpick a project and the next push takes its sessions off the
server. The dry run says how many sessions a push would remove.

How the client learns what the server holds is left to the server's author. The manifest
allows either:

- the server returns its `{path: sha256}` for this (user, machine) before the upload, and the
  client sends what differs; or
- the client remembers the last manifest the server accepted, and the server rejects a push
  whose manifest lists an object it neither holds nor was sent, naming the missing paths so
  the client sends them.

Neither is built. In a dry run, `--prev DIR` stands in: it takes the manifest of an earlier
bundle as what the server holds.

A server should check each received object against its `sha256` and `bytes`, and apply a
push whole or not at all.

Hashes are over the bytes after redaction. A debrief release that changes the redaction
patterns changes the bytes of every object the new patterns touch, so those objects are sent
again. That is intended: the server's copy then matches what the newer patterns allow.

Labels are part of the session object, so labeling a session sends that session again. So does a
change in a linked PR's state.

## Redaction

Every object, and the manifest's project list, goes through one function before it is hashed or
written. It replaces secret-shaped strings (GitHub, Anthropic, OpenAI, AWS, Slack, Stripe and
Google tokens, JWTs, private keys, bearer tokens, passwords in URLs) with `[redacted]`. It
works on patterns, so it misses a secret with no known shape.

The same function shortens home-folder paths in text (`/Users/<name>`, `/home/<name>`,
`C:\Users\<name>`) to `~`, so a transcript does not carry the user's login name that way.
The dry run reports both counts.

The text of a finding or an idea is written by a model. It is not checked for the names of
projects that were not picked; the rules under findings.json leave out the items most likely to
carry them.

## Left out, and why

- **Other people's sessions**: any session that names an author.
- **A separate index of the per-session counts**, apart from the transcripts: the first server
  does not need one, and a later format can add it.

- **Unpicked projects**, but for their count: a folder name can be a client's name.
- **Absolute folder paths** as fields: the server needs a project's identity, not where it sits on disk.
- **Raw logs, full tool input and the output of calls that worked, text past 8,000 characters
  of a message**: the local report does without them, and they are most of the bulk and most of
  the risk.
- **The evidence bundles sent to the labeling model, config.toml, model choices, API keys**:
  none of it is needed to show a report.
- **Derived tables of the local report** (flagged turns, hotspot files, skill uses): a server
  can work the first two out from the session objects. Skill uses are not derivable and are
  left for a later version.
- **A date window**: every session of a picked project is in. `sync --since` has no twin here.
- **Compression, transport, login, endpoints**: the server's author decides.

## Size

On the author's machine (2 picked projects, 221 sessions) a first push is about 13 MB before
compression: a session object has a median of 27 KB and the largest is 0.4 MB.
