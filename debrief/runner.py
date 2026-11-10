"""Run `debrief sync` as a child of `debrief open`: one at a time, watched, and stoppable.

The child leads its own process group, so stopping the group also stops the `claude -p`
calls it started. Its output goes to DEBRIEF_HOME/run.log, one file, overwritten each run.
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from debrief import config
from debrief.paths import HOME

SYNC = [sys.executable, "-u", "-m", "debrief.cli", "sync"]  # -u: a pipe would hold prints back
EXPORT = [sys.executable, "-m", "debrief.cli", "export"]
STAGE = re.compile(r"== ([\w-]+)")
COUNT = re.compile(r"\s*(\d+)/(\d+) done")
WOULD = re.compile(r"would label (\d+) items in (\d+) .*? calls(?:.*about (\d+) min)?")


def progress(lines, before: dict | None = None) -> dict:
    """Where a run is, from its output: {stage, done, total}. Lines it does not know change nothing."""
    at = {"stage": None, "done": None, "total": None} | (before or {})
    for line in lines:
        if m := STAGE.match(line):
            at = {"stage": m[1], "done": None, "total": None}
        elif m := COUNT.match(line):
            at |= {"done": int(m[1]), "total": int(m[2])}
    return at


def estimate_of(lines) -> dict:
    """The counts `sync --dry-run` printed; None for what it did not say."""
    m = next((m for line in lines if (m := WOULD.match(line))), None)
    items, calls, minutes = (int(x) if x else None for x in m.groups()) if m else (None, None, None)
    return {"items": items, "calls": calls, "minutes": minutes}


def stop_group(pid: int, wait: float = 5.0) -> None:
    """Ask the process group to stop; kill what is left after `wait` seconds."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):  # gone; macOS says EPERM for a group of zombies
                return
            sig = 0  # from here on only ask whether anyone is left
            time.sleep(0.05)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Runner:
    cmd = SYNC      # tests put a fake child here; nothing from HTTP reaches it
    export = EXPORT

    def __init__(self, home: Path = HOME, hide: tuple[str, ...] = ()):
        self.log, self.pidfile, self.hide = home / "run.log", home / "run.pid", hide
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.busy = False  # a run or an estimate is going
        self.cancelled = False
        self.tail: deque[str] = deque(maxlen=30)
        self.state = {"state": "idle", "started": None, "ended": None, "exit_code": None} | progress([])

    def status(self) -> dict:
        with self.lock:
            return self.state | {"log": list(self.tail)}

    def scrub(self, text: str) -> str:
        """Blank the page token and every OpenRouter key a stage could see."""
        keys = [src.get(config.KEY) for src in
                (os.environ, config.dotenv_values(config.ENV_PATH), config.dotenv_values(config.find_dotenv()))]
        for secret in (*self.hide, *keys):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text

    def stop_orphan(self) -> None:
        """Stop a run whose server died: it would go on calling the model with nobody watching."""
        try:
            pid = int(self.pidfile.read_text())
        except (OSError, ValueError):
            return
        ps = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout
        if " ".join(self.cmd[1:]) in ps:  # pids get reused: stop only what is this command
            stop_group(pid)
        self.pidfile.unlink(missing_ok=True)

    def start(self) -> bool:
        """Start a run. False when a run or an estimate is going."""
        with self.lock:
            if self.busy:
                return False
            self.stop_orphan()
            self.log.parent.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, errors="replace",
                                         start_new_session=True)
            self.pidfile.write_text(str(self.proc.pid))
            self.busy, self.cancelled = True, False
            self.tail.clear()
            self.state = {"state": "running", "started": now(), "ended": None, "exit_code": None} | progress([])
            self.reader = threading.Thread(target=self.read, args=(self.proc,), daemon=True)
            self.reader.start()
            return True

    def read(self, proc: subprocess.Popen) -> None:
        with open(self.log, "w") as log:
            for line in proc.stdout:
                line = self.scrub(line.rstrip("\n"))
                log.write(line + "\n")
                log.flush()
                with self.lock:
                    self.tail.append(line)
                    self.state |= progress([line], {k: self.state[k] for k in ("stage", "done", "total")})
        code = proc.wait()
        with self.lock:
            end = "cancelled" if self.cancelled else "failed" if code else "done"
            self.state |= {"state": end, "ended": now(), "exit_code": code}
            self.pidfile.unlink(missing_ok=True)
            self.busy = False

    def cancel(self) -> None:
        """Stop the run and everything it started, and wait until it is gone. Nothing to do when idle."""
        with self.lock:
            proc = self.proc
            if not proc or proc.poll() is not None:
                return
            self.cancelled = True
        stop_group(proc.pid)
        self.reader.join(10)

    def estimate(self) -> dict | None:
        """Run `sync --dry-run`, then export so the page matches what was ingested. None when busy."""
        with self.lock:
            if self.busy:
                return None
            self.busy = True
        try:
            lines, code = [], 0
            for cmd in ([*self.cmd, "--dry-run"], self.export):
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", check=False)
                lines += self.scrub(r.stdout).splitlines()
                code = r.returncode
                if code:
                    break
            return {"exit_code": code, **estimate_of(lines), "lines": lines}
        finally:
            with self.lock:
                self.busy = False
