# Server API, version 1

The contract between `debrief` (the CLI) and the hosted server. The server signs a user in with
GitHub, takes the bundle that `docs/upload-format.md` describes, lists it and deletes it. It
runs no models and, in this version, serves nothing back but the project list.

The server code is in `server/`. Nothing is deployed yet.

## Basics

- **Base URL.** Not fixed yet. The CLI reads it from config; every path below is relative to it.
  HTTPS only.
- **Version.** Every route starts with `/v1`. A change that breaks a client gets a new prefix.
  The server may add response fields at any time: a client ignores fields it does not know.
- **Bodies.** Request and response bodies are UTF-8 JSON (`Content-Type: application/json`),
  except the file upload, whose request body is the raw bytes of one object. No route takes a
  compressed request body.
- **Times** are UTC, RFC 3339, to the second: `2026-11-17T09:30:00Z`.
- **Ids and tokens** (`upload_id`, `device_code`, `token`, project `id`) are opaque strings of
  `[A-Za-z0-9_-]`, at most 200 characters. Do not parse them.
- Every response carries `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

### Auth header

Every route but the two sign-in routes needs

    Authorization: Bearer <token>

where `<token>` came from `POST /v1/auth/token`. A token lasts 90 days from sign-in and is not
renewed: when it runs out the user signs in again. A missing, unknown, revoked or expired token
gets `401 invalid_token`, whatever the route. The server never says which.

The user is always the owner of the token. No route takes a user id, and nothing in a bundle
names the user.

### Errors

Every response with a status of 400 or more has this body:

    {"error": {"code": "quota_exceeded", "message": "This push needs 612 MB; your quota is 500 MB."}}

`code` is stable and is what a client tests. `message` is English for a person and may change.
Some codes add fields inside `error`; they are listed with the route.

Codes any route can return:

| Status | `code` | Meaning |
|---|---|---|
| 400 | `bad_request` | The body is not JSON, or a field is missing or of the wrong type. |
| 401 | `invalid_token` | See "Auth header". Sign in again. |
| 404 | `not_found` | No such route, or no such thing owned by this user. |
| 405 | `method_not_allowed` | |
| 429 | `rate_limited` | Wait the number of seconds in the `Retry-After` header, then retry. |
| 500 | `internal` | Retrying is safe on every route. |

A thing that belongs to another user is `404 not_found`, the same as a thing that does not exist.

### Limits

| Limit | Value | When passed |
|---|---|---|
| Stored bytes per user, over all machines | 500,000,000 | `413 quota_exceeded` |
| Machines per user | 10 | `422 too_many_machines` |
| Manifest body | 1,000,000 bytes | `413 manifest_too_large` |
| Objects in one manifest | 5,000 | `422 invalid_manifest` |
| Projects in one manifest | 200 | `422 invalid_manifest` |
| One object | 25,000,000 bytes | `422 invalid_manifest` at create |
| Life of an upload | 1 hour from create | `404 not_found` |
| Life of a device code | GitHub's `expires_in`, 900 seconds today | `410 expired_device_code` |
| Life of a token | 90 days | `401 invalid_token` |
| `POST /v1/auth/device` | 10 a minute per IP address | `429 rate_limited` |
| All other routes | 600 a minute per user (per IP address for `POST /v1/auth/token`) | `429 rate_limited` |

Stored bytes are the sum of `bytes` over the objects in the user's committed manifests. The same
object held by two machines counts twice. An open upload counts too, in full, from the moment it
is created until it is committed, cancelled or expired: see `quota_exceeded` under
`POST /v1/uploads`. A machine counts toward the limit of 10 once it has a snapshot or an open
upload.

A client may send up to 4 file uploads at once.

## Sign-in

The server runs GitHub's device flow. The CLI never sees a GitHub token, and the server keeps
none: it reads the user's numeric GitHub id and login once and throws GitHub's token away.

### POST /v1/auth/device

Start a sign-in. No auth, no body.

`200`:

    {
     "device_code": "Zk3...",
     "user_code": "WDJB-MJHT",
     "verification_uri": "https://github.com/login/device",
     "interval": 5,
     "expires_in": 900
    }

- `device_code`: the server's own code, not GitHub's. Secret: whoever holds it gets the token.
- `user_code`, `verification_uri`: show both to the user, who types the code at that address.
- `interval`: seconds to wait between polls.
- `expires_in`: seconds until the codes die.

Errors: `429 rate_limited`; `502 github_unavailable` (GitHub did not answer, or the app's
device flow is off; retry later).

### POST /v1/auth/token

Poll for the outcome. No auth.

    {"device_code": "Zk3..."}

`202`, the user has not finished:

    {"status": "pending", "interval": 5}

Wait `interval` seconds before the next poll. It can grow between polls; always use the
latest. Polling sooner does no harm and gets the same answer.

`200`, signed in:

    {
     "token": "dbf_...",
     "expires_at": "2027-02-15T09:30:00Z",
     "user": {"id": "u_...", "github_id": 583231, "login": "octocat"}
    }

The token is shown this once; the server stores only its hash. The device code is now spent.
`user.id` is the server's id for the account. A first sign-in creates the account. `login` is
GitHub's at the time of this sign-in and may change at the next; `github_id` does not.

Errors:

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_device_code` | Unknown or already spent. Start again. |
| 403 | `access_denied` | The user refused at GitHub. Start again. |
| 410 | `expired_device_code` | The user took too long. Start again. |
| 502 | `github_unavailable` | Keep polling. |

### DELETE /v1/auth/token

Sign out: revoke the token that carries the request. Other tokens of the same user stay valid.

`204`, no body. The token gets `401 invalid_token` from then on.

## Upload

A push replaces the whole snapshot of one (user, machine), where `machine` is the manifest's
`machine`. It takes three steps: create, send the missing files, commit. Nothing changes on the
server until the commit, and the commit applies whole or not at all.

A retry after any failure is always the same: start again at create with the same manifest.
Files that arrived are kept for the life of the upload they were sent to, so the new create
lists them as held when it comes within that hour.

### POST /v1/uploads

Header, on a user's first upload:

    X-Debrief-Rights-Confirmed: 1

The CLI sends it once the user has confirmed they have the right to share what they upload
(see "Later pushes" in `docs/upload-format.md`). The first time the server sees it, it stores the
time on the user's account; from then on the header is not needed and changes nothing. A create
from a user with no stored time and no header gets `403 confirmation_required`. Only the exact
value `1` counts. Deleting the account forgets the confirmation.

Body: the `manifest.json` of `docs/upload-format.md`, format 1, with one addition:

- each entry of `objects` whose path starts with `sessions/` carries `"project"`, a `key` from
  the manifest's `projects`. It is the same value as the `project` field inside the session
  object. The server never parses objects, so this is how it knows which sessions to delete
  with a project.

      "objects": {
       "sessions/a1.json": {"sha256": "9f2c…", "bytes": 27415, "project": "octo/alpha"},
       "findings.json": {"sha256": "41aa…", "bytes": 18877}
      }

The server ignores `sent` (it works out what is missing itself) and any field it does not know.

What the server checks, each failure a `422 invalid_manifest` whose `message` names the field:

- `format` is the integer 1. Any other value is `422 unsupported_format` instead, with
  `"supported": [1]` in `error`.
- `tool` is a string of at most 100 characters.
- `machine` is a UUID in lower case: 8-4-4-4-12 hex digits.
- `redaction` and `unpicked` are integers, 0 or more.
- `projects` is an array. Each has a `key` (1 to 200 characters of `[A-Za-z0-9_.:/-]`), unique
  in the manifest; a `name` (string, at most 200 characters); `sessions` and `turns` (integers,
  0 or more); `analysed_at` (string of at most 40 characters, or null).
- `objects` is an object. Each path is `findings.json`, `github.json`, or
  `sessions/<name>.json` with `<name>` matching `[A-Za-z0-9_-][A-Za-z0-9_.-]*`, at most 200
  characters in all. `sha256` is 64 lower-case hex digits. `bytes` is an integer from 1 to
  25,000,000. A `sessions/` entry has a `project` that is one of the `projects` keys.
- `objects` is empty when `projects` is.
- When the server already holds the bytes of a `sha256` for this user, `bytes` is their true
  length. The quota is summed from `bytes`, so the server takes only sizes it has checked.

A manifest with `"projects": []` and `"objects": {}` is valid. Committing it removes
everything the server holds for that machine.

`201`:

    {
     "upload_id": "up_...",
     "missing": ["sessions/a1.json", "findings.json"],
     "expires_at": "2026-11-17T10:30:00Z"
    }

`missing` lists the manifest paths whose bytes the server does not hold for this user. It
holds an object when a file with that SHA-256 is in any of the user's committed snapshots, or
arrived in an upload that has not expired. Send exactly the paths in `missing`. It can be empty;
commit all the same.

Creating an upload cancels an earlier open upload of the same (user, machine). Files that
arrived for the cancelled upload are kept when the new manifest lists them and deleted when it
does not.

Errors, besides `422 invalid_manifest` and `422 unsupported_format`:

| Status | `code` | Extra fields in `error` | Meaning |
|---|---|---|---|
| 403 | `confirmation_required` | | See the header above. Checked before the body is read. |
| 413 | `manifest_too_large` | | The body is over 1,000,000 bytes. |
| 413 | `quota_exceeded` | `quota_bytes`, `used_bytes`, `needed_bytes` | `used_bytes` is what the user's other machines hold in snapshots plus everything their open uploads list; `needed_bytes` is the sum of `bytes` in this manifest. Their sum is over `quota_bytes`. An open upload of another machine frees its share when it is committed or expires. |
| 422 | `too_many_machines` | `machines` (the ids counted) | This would be the user's 11th machine. Delete the projects of one to free it; a machine that has only an open upload frees itself when the upload expires. |

### PUT /v1/uploads/{upload_id}/files/{path}

Send one object. `{path}` is the manifest path as it stands, slash included:
`/v1/uploads/up_abc/files/sessions/a1.json`.

- Body: the object's bytes, exactly as hashed. No compression, no multipart.
- `Content-Length` is required and must equal the manifest's `bytes` for that path.
- `Content-Type: application/json`.

`204`, no body: the bytes are stored and match the manifest's `sha256` and `bytes`.

Sending a path again, or a path that was not in `missing`, is allowed and gets the same answer.

Errors:

| Status | `code` | Meaning |
|---|---|---|
| 404 | `not_found` | The upload is unknown, expired, committed, cancelled or another user's; or the path is not in its manifest. On an unknown upload, start again at create. |
| 411 | `length_required` | No `Content-Length` (a chunked body). |
| 422 | `size_mismatch` | `Content-Length`, or the count of bytes that arrived, differs from the manifest's `bytes`. Nothing is stored. |
| 422 | `hash_mismatch` | The bytes do not hash to the manifest's `sha256`. Nothing is stored. |

### POST /v1/uploads/{upload_id}/commit

No body. Swaps the snapshot of the upload's (user, machine) for the manifest: every object the
manifest lists is now held under its path, every other object of that machine is gone, and the
machine's project list is the manifest's. One database transaction does the swap, so a reader
sees the old snapshot or the new one, never a mix. The upload is then spent.

`200`:

    {
     "machine": "0b9c4c0e-3f0a-4d0e-9d55-6a1f4b6f1f0a",
     "committed_at": "2026-11-17T09:41:07Z",
     "objects": 13,
     "bytes": 412770,
     "removed": 2,
     "projects": [ ...as in GET /v1/projects, this machine's only... ],
     "usage": {"bytes": 412770, "quota_bytes": 500000000}
    }

`objects` and `bytes` describe the new snapshot; `removed` counts paths the old snapshot had and
the new one lacks.

Files that no snapshot of the user lists any more are deleted from storage as part of the
commit. If that step fails the commit still stands and the files, which no route can reach, are
deleted by a sweep that runs at least once an hour.

Errors:

| Status | `code` | Extra fields in `error` | Meaning |
|---|---|---|---|
| 404 | `not_found` | | As for the file route. If a commit's answer was lost, start again at create: when the commit had landed, `missing` comes back empty and the second commit changes nothing. |
| 409 | `incomplete` | `missing` (paths) | Some listed objects have not arrived. Send them and commit again. Nothing changed. |
| 413 | `quota_exceeded` | as at create | Another machine's commit or new upload used the room since create. Nothing changed. |

## Projects

A project on the server is one entry of one machine's manifest: (user, machine, `key`). The same
repo pushed from two machines is two projects with the same `key`.

### GET /v1/projects

`200`:

    {
     "projects": [
      {
       "id": "p_5d1c0a9e44b07f21",
       "machine": "0b9c4c0e-3f0a-4d0e-9d55-6a1f4b6f1f0a",
       "key": "octo/alpha",
       "name": "alpha",
       "sessions": 12,
       "turns": 140,
       "analysed_at": "2026-01-05T11:00:00Z",
       "bytes": 391204,
       "pushed_at": "2026-11-17T09:41:07Z"
      }
     ],
     "usage": {"bytes": 412770, "quota_bytes": 500000000}
    }

- `id` is stable for a (user, machine, `key`) across pushes.
- `key`, `name`, `sessions`, `turns`, `analysed_at` are the manifest's.
- `bytes` is the sum over the project's session objects. `findings.json` and `github.json`
  belong to the machine, not to a project: they count in `usage.bytes` only.
- `pushed_at` is the time of the machine's last commit.
- Order: `pushed_at` newest first, then `key`.

A user with nothing stored gets `"projects": []`.

### DELETE /v1/projects/{id}

Deletes one project from the server:

- its session objects (the entries whose `project` is its `key`, on its machine);
- that machine's `findings.json` and `github.json`. They are shared by the machine's projects
  and may quote the deleted one, and the server cannot cut them. The next push sends them again;
- the project itself. When it was the machine's last, the machine no longer counts toward the
  limit of 10.

It also cancels the user's open uploads and deletes the files that arrived for them.

`204`, no body: the rows are gone and so are the stored files that no other snapshot of the user
lists. `404 not_found` when the id is unknown. On `500` nothing is promised but that a retry is
safe: the retry gets `204`, or `404` when the first try had got as far as removing the project.

The server does not remember the deletion. If the project is still picked on the machine, the
next push uploads it again: unpick it first (`[projects] include` in config.toml). Unpicking and
pushing also removes it, without this route; the route is for a machine the user no longer has.

### DELETE /v1/me

Deletes the account: every stored file, every snapshot, project and open upload, every token
(this one too), and the user's row with the GitHub id and login.

`204`, no body, once all of it is gone. On `500` the account still exists and the token still
works: retry.

Signing in again afterwards makes a new, empty account. Nothing is kept that could restore the
old one.

## What the server does with a file

- It never parses an object and never serves one as anything but `application/json`.
- It stores a file under the user's own prefix, named by its SHA-256, so the same bytes are
  stored once per user. Nothing is shared between users.
- A file that arrived for an upload which was never committed is deleted by the hourly sweep
  after the upload expires.
- No old snapshots are kept. Cloudflare's own backups are outside this API; the privacy policy
  states their window.

## Later

Not in the first server; the paths are placeholders:

- `GET /v1/me`: who the token belongs to.
- `POST /v1/auth/browser-link` and `GET /auth/link/{nonce}`: a one-time link that signs the
  browser in.
- `GET /r/{project}/` and `GET /r/{project}/data/{path}`: the server's own report site and the
  user's JSON.
- `DELETE /v1/machines/{machine}`: drop one machine's snapshot in one call.
- `Content-Encoding: gzip` on the file route.
- Teams.
