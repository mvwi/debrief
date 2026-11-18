# debrief server

The hosted side of `debrief push`: a Cloudflare Worker that signs a user in with GitHub, takes
their redacted bundle, lists it and deletes it. It runs no models and serves no report yet.

- The routes: [`docs/server-api.md`](../docs/server-api.md).
- What a bundle holds: [`docs/upload-format.md`](../docs/upload-format.md).

Nothing is deployed. Everything below "Deploy" is a list for whoever does that first.

## How it is put together

| File | What it does |
|---|---|
| `src/index.ts` | Routes, rate limits, the error shape, the hourly cron. |
| `src/auth.ts` | GitHub device flow, tokens. |
| `src/uploads.ts` | Manifest checks, file upload, commit. |
| `src/projects.ts` | Project list, project delete, account delete. |
| `src/storage.ts` | File names in R2, deleting unlisted files, the sweep. |
| `src/db.ts` | The only file that talks to D1. |
| `migrations/` | The D1 schema. |

Three rules hold the design up:

1. **Every query names its user.** A handler gets a `UserDb` whose user id is always SQL
   parameter `?1`; a query without `?1` throws. Only `src/db.ts` may prepare a statement, and a
   test fails if another file does. The few queries with no user (sign-in, the sweep) are named
   functions in that file.
2. **Files are named by their hash, under the user's prefix**: `u/<user id>/b/<sha256>`. A
   snapshot is rows in D1 that point at hashes, so a commit is one D1 transaction and R2 needs no
   rename. Nothing is shared between users.
3. **An upload never sits in memory.** The body streams to R2 through a `FixedLengthStream`
   (which fails the write on a wrong length) with R2's `sha256` check (which fails it on a wrong
   hash). The Worker does not parse an object.

## Run it locally

Needs Node 22.12 or later. All of this stays on your machine: local D1 and R2 live in
`.wrangler/`.

    cd server
    npm ci
    npm test                                        # vitest, in the Workers runtime
    npm run typecheck
    npx wrangler d1 migrations apply debrief --local
    npm run dev                                     # http://localhost:8787

Run wrangler from this folder, never from your home folder.

Sign-in under `npm run dev` needs a real GitHub App client id in `GITHUB_CLIENT_ID`: put
`GITHUB_CLIENT_ID=...` in a `.dev.vars` file here (git ignores it). The tests fake GitHub and
need nothing.

## Deploy

Not done yet. In order:

1. **GitHub App.** Register one with no permissions, no webhook, and "Enable Device Flow" ticked.
   Copy its client id (not the app id). No client secret is needed.
2. **Bucket, in the EU.** A jurisdiction is fixed when the bucket is made:

       npx wrangler r2 bucket create debrief-bundles --jurisdiction eu

   `wrangler.jsonc` already binds it with `"jurisdiction": "eu"`.
3. **Database, in the EU.** Same rule:

       npx wrangler d1 create debrief --jurisdiction=eu

   Put the `database_id` it prints into `wrangler.jsonc`.
4. **Client id.** Set `vars.GITHUB_CLIENT_ID` in `wrangler.jsonc`. It is public, not a secret.
5. **Schema.**

       npx wrangler d1 migrations apply debrief --remote

6. **Plan.** Use Workers Paid. The free plan allows 50 D1 queries and subrequests per request,
   which the sweep passes on a bucket of any size.
7. **Ship.**

       npx wrangler deploy

8. **Check.** The cron trigger (`17 * * * *`) shows in the dashboard; `POST /v1/auth/device`
   returns a user code; a sign-in with your own account works with the app installed nowhere.
9. **Tell the CLI.** The base URL goes in the CLI's config; it has no default.

Also before real users: terms and a privacy policy that name Cloudflare and the backup window
below, and a route or custom domain.

## Known limits

- **Device-flow phishing.** Someone can start a sign-in and talk a victim into typing the code
  at GitHub; the attacker's CLI then holds a token for the victim's account. GitHub's prompt,
  which names the app, is the only defence. Today that token can delete the victim's upload or
  push junk; once the server shows reports it could read them.
- **GitHub's token outlives the sign-in.** The server reads the user's id and login with it and
  drops it, but cannot revoke it without a client secret, which it does not hold. The token has
  no permissions and GitHub ends it after 8 hours.
- **Deleted rows stay in D1's point-in-time recovery** for 30 days on Workers Paid (7 on free).
  Those rows hold GitHub ids, logins, project names and keys, and file hashes. Transcripts are
  only in R2, where a delete is a delete. **The privacy policy must say this**: after a user
  deletes a project or their account, that account data can be restored for up to 30 days.
- **A deleted project comes back** on the next push if it is still picked on the machine. The
  server keeps no memory of deletions.
- **Rate limits are rough.** Cloudflare counts them per location and syncs in the background, so
  a caller can pass a limit for a moment.
- **One race loses a file.** Deleting files that nothing lists checks, then deletes. A user who
  pushes the same bytes in the milliseconds between the two keeps a row whose file is gone. The
  fix is a per-user lock; see `collect` in `src/storage.ts`.
- **The sweep lists the whole bucket** every hour. Fine for thousands of users, not for millions.
- **The quota bounds storage loosely, not exactly.** Counted bytes can never pass 500 MB, and
  every counted size is one the server checked. Real storage can run higher for a while: a
  commit holds the old and the new snapshot of a machine until it lands, and the files of an
  upload that expired stay until the next sweep. That is a few times the quota at worst, and it
  does not grow.
- **Not checked against real Cloudflare.** The tests run on the local simulators. How real R2
  fails a wrong hash or a short body is tested only there.
