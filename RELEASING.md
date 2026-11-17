# Releasing

Pushing a `v*` tag runs `.github/workflows/release.yml`: it tests, builds, publishes to PyPI,
creates the GitHub release and points the Homebrew formula at the new sdist.

The repo is `OWNER/debrief` below; swap in the real owner. The tap is `mvwi/homebrew-tap`.

## One-time setup

1. **PyPI trusted publisher.** The project does not exist on PyPI yet, so add a pending
   publisher: pypi.org, account sidebar, Publishing, GitHub Actions tab.

   | Field | Value |
   |---|---|
   | PyPI project name | `debrief-cli` |
   | Owner | `OWNER` |
   | Repository name | `debrief` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not hold the name. Someone else can still take `debrief-cli`
   until the first release lands.

2. **GitHub environment.** In `OWNER/debrief`: Settings, Environments, new environment `pypi`.
   Optional: limit its deployment tags to `v*`.

3. **Tap token.** Create a fine-grained personal access token (github.com, Settings,
   Developer settings): repository access "Only select repositories" with `mvwi/homebrew-tap`
   alone, permission Contents: Read and write, nothing else. Save it in `OWNER/debrief` as the
   Actions secret `TAP_TOKEN`.

   The expiry defaults to 30 days. Pick a date you will remember or "No expiration"; when it
   lapses the `tap` job fails until you replace the secret. A deploy key on the tap (write
   access) never expires and is as narrow, but you make the key pair by hand and change the
   tap checkout in `release.yml` from `token:` to `ssh-key:`.

4. **The formula** must exist at `Formula/debrief.rb` in the tap before the first tag, with its
   own `url` and `sha256` above any `resource` block. The workflow rewrites the first of each.

## Each release

1. Set `version` in `pyproject.toml`, run `uv lock`, commit, push to `main`. Wait for CI.
2. Tag and push:

       git tag v0.2.0
       git push origin v0.2.0

3. If the runtime dependencies changed since the last tag, refresh the formula's resource
   blocks by hand once the workflow is done. To tell: `git diff v0.1.0 v0.2.0 -- uv.lock`
   shows more than debrief's own version line and dev tools (pytest, ruff). Then:

       brew update-python-resources mvwi/tap/debrief
       cd (brew --repository mvwi/tap); git commit -am "debrief: update resources"; git push

A pre-release version (`0.2.0rc1`, tag `v0.2.0rc1`) goes to PyPI and gets a GitHub pre-release;
the formula stays as it is.

## When it fails

- **Tag does not match the version, or tests fail:** nothing went out. Delete the tag, fix,
  tag again.
- **PyPI published, a later job failed:** re-run the failed job from the Actions page. Do not
  re-tag: PyPI refuses a second upload of the same version.
- **A bad release is on PyPI:** yank it there and release the next version. A version number
  cannot be used twice.

## Renaming the package

Change `DIST_NAME` in `release.yml`, `name` in `pyproject.toml`, and add a trusted publisher
for the new PyPI name.
