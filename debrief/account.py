"""Sign in to a team server, and the plumbing `debrief push` uses to talk to it.

    debrief login     sign in with GitHub; the token is kept in DEBRIEF_HOME/credentials
    debrief logout    revoke that token and remove the file

The server's address is `[server] url` in config.toml, or DEBRIEF_SERVER. There is no default.
docs/server-api.md is the contract.
"""

import argparse
import contextlib
import json
import os
import sys
import time

import httpx

from debrief import config
from debrief.paths import HOME, NAME, SERVER_ENV

CREDENTIALS = HOME / "credentials"
TRANSPORT = None  # tests put a fake server here
TRIES = 5  # of one request the server answers with rate_limited

WHY = {
    "invalid_token": f"you are not signed in, or the sign-in has run out. Run `{NAME} login`.",
    "rate_limited": "the server is taking no more requests for now. Try again in a minute.",
    "github_unavailable": "GitHub did not answer the server. Try again later.",
    "invalid_device_code": f"the server no longer knows this sign-in. Run `{NAME} login` to start again.",
    "access_denied": f"you refused the sign-in at GitHub. Run `{NAME} login` to start again.",
    "expired_device_code": f"the code ran out before the sign-in was done. Run `{NAME} login` to start again.",
}


class Refused(Exception):
    """An answer with a status of 400 or more. `error` is the contract's error object."""

    def __init__(self, status: int, error: dict):
        super().__init__(f"{status} {plain(error.get('code'))}: {plain(error.get('message'))}")
        self.status, self.code, self.error = status, error.get("code"), error


def plain(text) -> str:
    """Text from the server, made safe to print: no control characters, not too long."""
    return "".join(c for c in str(text) if c.isprintable())[:300]


def why(e: Refused) -> str:
    """What to tell the user about a refusal."""
    quota = [e.error.get(k) for k in ("needed_bytes", "used_bytes", "quota_bytes")]
    if e.code == "quota_exceeded" and all(isinstance(n, int) for n in quota):
        return (f"{NAME}: this push needs {quota[0]:,} bytes and your other machines hold {quota[1]:,}; "
                f"the server keeps at most {quota[2]:,}. Nothing changed there. "
                f"Take a project out (`{NAME} config --drop DIR`) and push again.")
    return f"{NAME}: {WHY.get(e.code) or f'the server refused ({e})'}"


def server() -> str:
    """The server's address, or exit saying how to set one."""
    url = config.server()
    if not url:
        sys.exit(f"{NAME}: no server is set, so there is nowhere to send anything. Add\n\n"
                 f'    [server]\n    url = "https://..."\n\n'
                 f"to {config.CONFIG_PATH}, or set {SERVER_ENV}. `{NAME} push --dry-run` needs neither.")
    return url


def credentials() -> dict:
    """What `login` saved: server, token, login, expires_at. Empty when not signed in."""
    try:
        got = json.loads(CREDENTIALS.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def signed_in(url: str) -> dict:
    """The credentials for this server, or exit. A token is only ever sent to the server that gave it."""
    creds = credentials()
    if creds.get("server") != url or not creds.get("token"):
        sys.exit(f"{NAME}: not signed in to {url}. Run `{NAME} login`.")
    return creds


def client(url: str, token: str | None = None) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=60, transport=TRANSPORT,
                        headers={"Authorization": f"Bearer {token}"} if token else {})


def call(http: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    """One request. Waits as long as a rate_limited answer says and asks again; raises Refused on an error."""
    for left in reversed(range(TRIES)):
        r = http.request(method, path, **kwargs)
        if r.status_code != 429 or not left:
            break
        after = r.headers.get("Retry-After", "")
        wait = int(after) if after.isdecimal() else 5
        print(f"  the server asks for a pause of {wait} s ...", flush=True)
        time.sleep(wait)
    if r.status_code >= 400:
        try:
            error = r.json()["error"]
        except (ValueError, KeyError, TypeError):
            error = None
        raise Refused(r.status_code, error if isinstance(error, dict) else {"code": "", "message": "no reason given"})
    return r


@contextlib.contextmanager
def talking(url: str):
    """Exit with one plain line when the server refuses, cannot be reached or answers nonsense."""
    try:
        yield
    except Refused as e:
        sys.exit(why(e))
    except httpx.HTTPError as e:
        sys.exit(f"{NAME}: cannot reach {url} ({e or type(e).__name__})")
    except (KeyError, TypeError, ValueError) as e:  # an answer without the fields the contract names
        sys.exit(f"{NAME}: {url} sent an answer {NAME} does not understand ({e!r})")


def save(creds: dict) -> None:
    """Keep the token in DEBRIEF_HOME/credentials, readable only by its owner."""
    HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an older file may have been wider
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)


def login(argv: list[str] | None = None) -> None:
    """Sign in to the team server with GitHub. Prints a code to enter at github.com, then waits."""
    argparse.ArgumentParser(prog=f"{NAME} login", description=login.__doc__).parse_args(argv)
    url = server()
    with talking(url), client(url) as http:
        start = call(http, "POST", "/v1/auth/device").json()
        code, wait, left = start["device_code"], int(start["interval"]), int(start["expires_in"])
        print(f"Open {plain(start['verification_uri'])} and enter the code {plain(start['user_code'])}")
        print("Waiting for you to finish there (Ctrl-C to give up) ...", flush=True)
        while True:
            if left <= 0:  # the server would say the same at the next poll
                raise Refused(410, {"code": "expired_device_code"})
            wait = max(wait, 1)
            time.sleep(wait)
            left -= wait
            try:
                r = call(http, "POST", "/v1/auth/token", json={"device_code": code})
            except Refused as e:
                if e.code != "github_unavailable":  # which the contract says to poll through
                    raise
                continue
            got = r.json()
            if r.status_code == 200:
                break
            wait = int(got.get("interval", wait))
        who = got["user"]["login"]
        save({"server": url, "token": got["token"], "login": who, "expires_at": got["expires_at"]})
    print(f"Signed in to {url} as {plain(who)}, until {plain(got['expires_at'])}. The token is kept in {CREDENTIALS}.")


def logout(argv: list[str] | None = None) -> None:
    """Sign out: ask the server to revoke this machine's token, then remove it from this machine."""
    argparse.ArgumentParser(prog=f"{NAME} logout", description=logout.__doc__).parse_args(argv)
    if not CREDENTIALS.exists():
        print("Not signed in: there is no token on this machine.")
        return
    creds = credentials()
    try:
        with client(creds["server"], creds["token"]) as http:
            call(http, "DELETE", "/v1/auth/token")
        told = "The server has revoked the token."
    # The file below must go whatever happens here, a credentials file edited by hand included.
    except (Refused, httpx.HTTPError, httpx.InvalidURL, KeyError, TypeError) as e:
        if isinstance(e, Refused) and e.code == "invalid_token":
            told = "The server had already dropped the token."
        else:
            told = (f"The server was not told ({plain(e) or type(e).__name__}): "
                    "the token still works there until it runs out, 90 days after you signed in.")
    CREDENTIALS.unlink()
    print(f"Signed out: removed {CREDENTIALS}. {told}")
