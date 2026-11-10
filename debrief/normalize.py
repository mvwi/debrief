"""Load Entire's raw/<session_id>/ folders into the sessions, checkpoints, messages and tool_calls tables.

Logs read straight from disk (Claude Code, Codex, Pi) go through debrief.sources, which
uses the parsers and write_log here.

Messages and tool calls come from the agent's native log (raw.jsonl), which keeps tool
output; checkpoints come from transcript.json and session metadata from listing.json.
When the raw log is missing (too big to fetch), messages and tool calls come from
transcript.json instead, mostly without tool output.
Re-running replaces each session's rows.

    uv run python -m debrief.normalize
    uv run python -m debrief.normalize --ids efd45cc6-c764-4e22-80eb-cdbc1f9173f9
"""

import argparse
import gzip
import json
import posixpath
import re
import shlex
from datetime import datetime
from pathlib import Path

from debrief import db
from debrief.paths import RAW_DIR

OUTPUT_LIMIT = 4000
# Raise this when a change here alters the rows a log produces: debrief.sources then reads
# every log again, changed on disk or not. (main() below reloads Entire folders on every run.)
PARSER_VERSION = 2

# Text the harness puts in a user turn. A user message starting with one of these is "injected".
INJECTED_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>", "<local-command-stdout>",
    "<local-command-stderr>", "<local-command-caveat>", "Caveat: The messages below",
    "<system-reminder>", "<task-notification>", "<user-prompt-submit-hook>", "<bash-stdout>",
    "<bash-stderr>", "[Request interrupted by user", "Base directory for this skill",
    "This session is being continued from a previous conversation", "Another Claude session sent a message",
    "# AGENTS.md instructions", "<environment_context>", "<turn_aborted>", "<user_instructions>",
    # Claude Code /goal hooks and Codex goal turns: the harness restates or enforces a goal.
    "Stop hook feedback:", "A session-scoped Stop hook is now active", "<codex_internal_context",
)

# Tools whose path argument names a file they read or write (lower-cased names).
FILE_TOOLS = {"read", "edit", "write", "multiedit", "notebookedit"}
WRITE_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch"}
# Codex scripts embed the patch in a JS string, so a header can end at a literal "\\n".
PATCH_HEADER = re.compile(r"\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*(?=\\n|\n|\"|$)")
# Tools that run a shell command; shell_writes() finds the files such a command writes.
SHELL_TOOLS = {"bash", "shell", "exec_command", "local_shell"}
# A heredoc: the body is data, not commands, so it goes before the command is read.
HEREDOC = re.compile(r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*)\n.*?\n[ \t]*\3[ \t]*(?=\n|$)", re.DOTALL)
SHELL_BREAK = {"|", "||", "&&", ";", ";;", "&", "|&", "(", ")"}
# Redirect targets that are not files worth counting: devices and the system's temp folders.
NOT_A_FILE = re.compile(r"/dev/|/tmp/|/private/tmp/|/var/folders/|/private/var/folders/")
# A shell command inside a Codex exec script, as a plain double-quoted string.
SCRIPT_CMD = re.compile(r'\bcmd\s*:\s*("(?:[^"\\\n]|\\.)*")')
SCRIPT_WORKDIR = re.compile(r'\bworkdir\s*:\s*("(?:[^"\\\n]|\\.)*")')
# Paths inside a git worktree other than the session's cwd, e.g. .../.worktrees/<name>/cmd/x.go.
WORKTREE = re.compile(r".*/(?:\.worktrees/[^/]+|\.herdr/worktrees/[^/]+/[^/]+)/")
# Commands for which exit 1 means "nothing matched" or "false" rather than failure.
# find also exits 1 on real errors, so it only counts when the output shows none.
SEARCH_CMD = re.compile(r"(?:\w+=\S*\s+)*(?:rg|grep|egrep|fgrep|git\s+grep|test|\[)(?=\s|$)")
FIND_CMD = re.compile(r"(?:\w+=\S*\s+)*(?:rg|grep|egrep|fgrep|git\s+grep|find|test|\[)(?=\s|$)")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\"")
SHELL_SPLIT = re.compile(r"&&|\|\||[;|\n]")
HOUSEKEEPING_COMMANDS = {
    "login", "logout", "clear", "compact", "model", "config", "status", "cost", "resume",
    "exit", "help", "mcp", "permissions", "memory", "doctor", "fast", "effort", "context", "copy", "reload-skills",
}
DUPLICATE_WINDOW = 60  # seconds within which an identical repeated user message counts once
SESSION_ID = re.compile(r"[0-9a-zA-Z_-]{1,64}")
ERROR_TEXT = re.compile(r"error|fatal|panic|traceback|no such file|permission denied|\bFAIL\b", re.IGNORECASE)


class Log:
    """Messages and tool calls collected from one raw log, in order."""

    def __init__(self) -> None:
        self.cwd = ""
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self.by_id: dict[str, dict] = {}

    def add_message(self, role: str, ts: str, text: str, user_kind: str | None = None) -> int:
        last = self.messages[-1] if self.messages else None
        if role == "user" and last and last["role"] == "user" and last["text"] == text and within(last["ts"], ts):
            return len(self.messages) - 1  # the same prompt submitted again before any reply
        self.messages.append({"role": role, "user_kind": user_kind, "ts": ts, "text": text})
        return len(self.messages) - 1

    def assistant_idx(self, ts: str) -> int:
        """The assistant message a tool call belongs to: the last message, or a new empty one."""
        if self.messages and self.messages[-1]["role"] == "assistant":
            return len(self.messages) - 1
        return self.add_message("assistant", ts, "")

    def add_call(self, msg_idx: int, call_id: str, name: str, args, patch: str = "") -> dict:
        files, written = [], []
        if isinstance(args, dict):
            if name.lower() in FILE_TOOLS:
                path = args.get("file_path") or args.get("path") or args.get("notebook_path")
                if path:
                    files.append(path)
            elif name.lower() in SHELL_TOOLS:
                command = args.get("command") or args.get("cmd")
                if isinstance(command, list):  # ["bash", "-lc", script], or the argv itself
                    command = command[-1] if command[:1] in (["bash"], ["sh"], ["zsh"]) else shlex.join(map(str, command))
                workdir = args.get("workdir") if isinstance(args.get("workdir"), str) else ""
                written = shell_writes(command if isinstance(command, str) else "", workdir or self.cwd)
            elif name == "apply_patch" and not patch:  # the patch as a plain argument
                patch = next((v for v in (args.get("input"), args.get("patch")) if isinstance(v, str)), "")
        if name == "exec":  # a Codex script: read the shell commands it passes as plain strings
            workdirs = set(SCRIPT_WORKDIR.findall(patch))
            workdir = unquote(workdirs.pop()) if len(workdirs) == 1 else ""
            for literal in SCRIPT_CMD.findall(patch):
                written += shell_writes(unquote(literal), workdir or self.cwd)
        files += written + PATCH_HEADER.findall(patch)
        call = {
            "msg_idx": msg_idx, "name": name, "input": json.dumps(args), "output": None,
            "is_error": 0, "files": files, "writes": int(name.lower() in WRITE_TOOLS or bool(written)),
            "exits": [],  # (exit code, real failure) for each shell command the call ran
        }
        self.calls.append(call)
        self.by_id[call_id] = call
        return call

    def set_output(self, call_id: str | None, output: str, is_error: bool) -> None:
        call = self.by_id.get(call_id)
        if call:
            call["output"] = truncate(output)
            call["is_error"] = int(bool(is_error) or call["is_error"])

    def add_exit(self, call: dict | None, code: int, command: str, output: str) -> None:
        if call is not None:
            call["exits"].append((code, code != 0 and not quiet_search(code, command, output)))


def unquote(literal: str) -> str:
    """The value of a double-quoted string literal, or "" when it is not plain JSON."""
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        return ""


def literal_path(token: str) -> bool:
    """A token that names one file outright: plain path characters only, so no variable,
    substitution, glob or space, and not a device or temp file."""
    return bool(re.fullmatch(r"[\w./~@+-]*[\w/]", token)) and not NOT_A_FILE.match(token)


def shell_writes(command: str, cwd: str = "") -> list[str]:
    """Files a shell command writes, for the forms that name the file in the command text:
    `> path` and `>> path` (so `cat > path <<EOF` and `echo x > path` too), `tee path`,
    `sed -i ... path`, and an apply_patch heredoc. A relative path is taken from the last
    `cd` to a literal directory in the command, else from cwd. Anything else a command may
    write (python or perl scripts, cp, mv, git, a path held in a variable) is left out
    rather than guessed. Returns [] for a command that cannot be split into words."""
    found = PATCH_HEADER.findall(command) if "apply_patch" in command else []
    text = HEREDOC.sub(r"\1", command).replace("\\\n", " ")
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # an unclosed quote: not something to guess at
        tokens = []
    here, words = "", []  # the directory a `cd` moved to; the words of the simple command so far

    def end_command() -> None:
        nonlocal here
        name, args = (words[0], words[1:]) if words else ("", [])
        if name == "cd" and len(args) == 1 and literal_path(args[0]):
            here = posixpath.join(here, args[0])
        elif name == "tee":
            found.extend(posixpath.join(here, a) for a in args if not a.startswith("-") and literal_path(a))
        elif name == "sed" and any(re.match(r"-[a-zA-Z]*i|--in-place", a) for a in args):
            # Operands are the script, then the files; with -e or -f every operand is a file.
            operands, skip = [], False
            for i, a in enumerate(args):
                if skip or not a:
                    skip = False
                elif a in ("-e", "-f"):
                    skip = True
                elif not a.startswith("-") and not (a.startswith(".") and "/" not in a and re.match(r"-[a-zA-Z]*i$", args[i - 1])):
                    operands.append(a)  # not an option, nor BSD sed's backup suffix after -i
            has_script_option = any(a in ("-e", "-f") or a.startswith(("--expression", "--file")) for a in args)
            found.extend(posixpath.join(here, a) for a in operands[0 if has_script_option else 1:] if literal_path(a))
        words.clear()

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in (">", ">>") and i + 1 < len(tokens):
            if literal_path(tokens[i + 1]):
                found.append(posixpath.join(here, tokens[i + 1]))
            if words and words[-1].isdigit():  # the 2 of `2> path` is not an argument
                words.pop()
            i += 1
        elif t in SHELL_BREAK:
            end_command()
        elif not set(t) <= set("<>&|;()"):  # other redirects (<, <<, >&, &>) are skipped, target and all
            words.append(t)
        elif i + 1 < len(tokens):
            i += 1
        i += 1
    end_command()
    # "~/x" is left as written: the log does not say where home is.
    return list(dict.fromkeys(f if f.startswith("~") or not cwd else posixpath.normpath(posixpath.join(cwd, f)) for f in found))


def quiet_search(code: int, command: str, output: str) -> bool:
    """Exit 1 that means a search matched nothing: the last command was rg/grep/test, or a
    search command ran and the output shows no error."""
    if code != 1:
        return False
    unquoted = QUOTED.sub("''", command)  # so a | inside a pattern does not split the command
    segments = [s.strip().lstrip("(! ") for s in SHELL_SPLIT.split(unquoted) if s.strip()]
    if segments and SEARCH_CMD.match(segments[-1]):
        return True
    return any(FIND_CMD.match(s) for s in segments) and not ERROR_TEXT.search(output)


def exit_status(call: dict) -> tuple[int | None, int]:
    """(exit_code, is_error) for a call: the first real failure's code wins, else the highest code."""
    exits = call["exits"]
    failed = [code for code, real in exits if real]
    code = failed[0] if failed else max((c for c, _ in exits), default=None)
    if call["is_error"] is None and not exits:  # unknown: the log kept no result
        return None, None
    return code, int(call["is_error"] or bool(failed))


def within(earlier: str | None, later: str | None, seconds: int = DUPLICATE_WINDOW) -> bool:
    a, b = parse_ts(earlier), parse_ts(later)
    return bool(a and b) and abs((b - a).total_seconds()) <= seconds


def truncate(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    half = OUTPUT_LIMIT // 2
    return f"{text[:half]}\n... [{len(text) - OUTPUT_LIMIT} chars cut] ...\n{text[-half:]}"


def block_text(content, types=("text",)) -> str:
    """Join the text blocks of a message content field (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") in types)


def looks_injected(text: str) -> bool:
    return text.lstrip().startswith(INJECTED_PREFIXES)


def safe_session_id(session_id: object) -> bool:
    """A session ID names a folder under RAW_DIR and keys every table, so it may hold only
    letters, digits, "_" and "-": nothing that could climb out of RAW_DIR."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return False
    return (RAW_DIR / session_id).resolve().parent == RAW_DIR.resolve()


def read_jsonl(path: Path, tolerant: bool = False) -> list[dict]:
    """Read path, or path + '.gz' when scrape.py saved it compressed. tolerant skips lines
    that are not JSON: an agent still writing its log can leave a cut-off last line."""
    gz = path.with_name(path.name + ".gz")
    text = gzip.decompress(gz.read_bytes()).decode() if gz.exists() else path.read_text()
    rows = []
    # Split on "\n" only: splitlines() also breaks on U+2028 and friends, which can sit
    # inside JSON strings (pasted text) and would cut a record in two.
    for line in text.split("\n"):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if not tolerant:
                    raise
    return rows


# --- Claude Code -------------------------------------------------------------------------------

def slash_command(text: str) -> str | None:
    """'/name args' for a slash command a person typed to invoke a skill or custom command.
    Built-in housekeeping commands (/login, /clear, ...) return None: they carry no intent."""
    name = re.search(r"<command-name>/?([^<]+)</command-name>", text)
    if not name or name.group(1).strip() in HOUSEKEEPING_COMMANDS:
        return None
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.DOTALL)
    return f"/{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()


def claude_user_kind(entry: dict, text: str) -> str:
    if entry.get("isMeta") or entry.get("isCompactSummary") or looks_injected(text):
        return "injected"
    origin = entry.get("origin")
    if origin is not None:  # newer logs say who wrote the turn
        return "human" if origin.get("kind") == "human" else "injected"
    return "human"


def parse_claude(entries: list[dict]) -> Log:
    log = Log()
    last_api_id = None  # Claude Code logs one entry per content block; merge them by message.id
    for e in entries:
        if e.get("isSidechain"):
            continue
        log.cwd = log.cwd or e.get("cwd", "")
        kind, ts = e.get("type"), e.get("timestamp")
        if kind == "assistant":
            msg = e["message"]
            if msg.get("model") == "<synthetic>":  # harness error text such as "API Error: ..."
                continue
            text = block_text(msg["content"])
            if msg.get("id") == last_api_id and log.messages and log.messages[-1]["role"] == "assistant":
                idx = len(log.messages) - 1
                log.messages[idx]["text"] = "\n".join(t for t in (log.messages[idx]["text"], text) if t)
            else:
                idx = log.add_message("assistant", ts, text)
            last_api_id = msg.get("id")
            for block in msg["content"]:
                if block.get("type") == "tool_use":
                    log.add_call(idx, block["id"], block["name"], block.get("input"))
        elif kind == "user":
            content = e["message"]["content"]
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    claude_result(log, block)
            text = block_text(content)
            command = slash_command(text) if not e.get("isMeta") else None
            if command and (e.get("origin") or {}).get("kind", "human") == "human":
                log.add_message("user", ts, command, "human")
            elif text.strip():
                log.add_message("user", ts, text, claude_user_kind(e, text))
        elif kind == "attachment" and e["attachment"].get("type") == "queued_command":
            # A prompt typed while the agent was busy, handed to it mid-turn.
            att = e["attachment"]
            text = block_text(att.get("prompt"))
            if text.strip():
                human = (att.get("origin") or {}).get("kind") == "human" and not looks_injected(text)
                log.add_message("user", ts, text, "human" if human else "injected")
        elif kind == "system" and e.get("subtype") == "local_command":
            log.add_message("user", ts, e.get("content", ""), "injected")
    return log


CLAUDE_EXIT = re.compile(r"\AExit code (\d+)")


def claude_result(log: Log, block: dict) -> None:
    call_id, output = block["tool_use_id"], block_text(block.get("content"))
    call = log.by_id.get(call_id)
    m = CLAUDE_EXIT.match(output)
    if call and call["name"] == "Bash" and (m or not block.get("is_error")):
        log.set_output(call_id, output, False)
        log.add_exit(call, int(m.group(1)) if m else 0, json.loads(call["input"]).get("command", ""), output)
    else:
        log.set_output(call_id, output, block.get("is_error"))


# --- Pi ----------------------------------------------------------------------------------------

def parse_pi(entries: list[dict]) -> Log:
    log = Log()
    for e in entries:
        kind, ts = e.get("type"), e.get("timestamp")
        if kind == "session":
            log.cwd = e.get("cwd", "")
        elif kind == "message":
            msg = e["message"]
            role = msg.get("role")
            if role == "user":
                text = block_text(msg["content"])
                if text.strip():
                    log.add_message("user", ts, text, "injected" if looks_injected(text) else "human")
            elif role == "assistant":
                idx = log.add_message("assistant", ts, block_text(msg["content"]))
                for block in msg["content"]:
                    if block.get("type") == "toolCall":
                        args = block.get("arguments") or {}
                        patch = args.get("patch") or args.get("input") if isinstance(args, dict) else None
                        log.add_call(idx, block["id"], block["name"], args, patch if isinstance(patch, str) else "")
            elif role == "toolResult":
                pi_result(log, msg)
        elif kind == "custom_message":  # extension context sent to the model as a user turn
            text = block_text(e.get("content"))
            if text.strip():
                log.add_message("user", ts, text, "injected")
        elif kind == "compaction":
            log.add_message("user", ts, e.get("summary", ""), "injected")
    return log


PI_EXIT = re.compile(r"Command exited with code (\d+)\s*\Z")


def pi_result(log: Log, msg: dict) -> None:
    call_id, output = msg["toolCallId"], block_text(msg.get("content"))
    call = log.by_id.get(call_id)
    m = PI_EXIT.search(output)
    if call and call["name"] == "bash" and (m or not msg.get("isError")):
        log.set_output(call_id, output, False)
        log.add_exit(call, int(m.group(1)) if m else 0, json.loads(call["input"]).get("command", ""), output)
    else:
        log.set_output(call_id, output, msg.get("isError"))


# --- Codex -------------------------------------------------------------------------------------

def parse_codex(entries: list[dict]) -> Log:
    log = Log()
    # item_completed UserMessage events hold exactly what the person typed.
    typed = {
        block_text(p["item"].get("content")).strip()
        for p in (e.get("payload") or {} for e in entries)
        if p.get("type") == "item_completed" and p["item"].get("type") == "UserMessage"
    }
    last_exec = None  # command and file-change events belong to the exec script that ran them
    for e in entries:
        p, ts = e.get("payload") or {}, e.get("timestamp")
        if e.get("type") == "session_meta":
            log.cwd = p.get("cwd", "")
        elif e.get("type") == "response_item":
            kind = p.get("type")
            if kind == "message" and p.get("role") == "user":
                text = block_text(p["content"], ("input_text",))
                # A <skill> block is Codex expanding a $skill the person typed; that typed
                # message is its own human turn, so the expansion counts as injected.
                human = not text.lstrip().startswith("<skill>") and (
                    text.strip() in typed if typed else not looks_injected(text))
                log.add_message("user", ts, text, "human" if human else "injected")
            elif kind == "message" and p.get("role") == "assistant":
                log.add_message("assistant", ts, block_text(p["content"], ("output_text",)))
            elif kind == "agent_message":  # a sub-agent reporting back
                log.add_message("user", ts, block_text(p.get("content"), ("input_text",)), "injected")
            elif kind == "custom_tool_call":
                script = p.get("input", "")
                name = "apply_patch" if "tools.apply_patch" in script else p["name"]
                last_exec = log.add_call(log.assistant_idx(ts), p["call_id"], name, {"input": script}, script)
            elif kind == "function_call":
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"arguments": p.get("arguments")}
                log.add_call(log.assistant_idx(ts), p["call_id"], p["name"], args)
            elif kind in ("custom_tool_call_output", "function_call_output"):
                out = p.get("output")
                text = out if isinstance(out, str) else block_text(out, ("input_text", "output_text", "text"))
                log.set_output(p.get("call_id"), text, text.startswith("Script failed"))
        elif e.get("type") == "event_msg" and p.get("type") == "item_completed" and last_exec:
            item = p["item"]
            if item.get("type") == "CommandExecution" and item.get("exit_code") is not None:
                command = item.get("command") or []
                command = command[-1] if isinstance(command, list) and command else str(command)
                log.add_exit(last_exec, item["exit_code"], command, item.get("aggregated_output") or "")
            elif item.get("type") == "FileChange":
                last_exec["files"] += list(item.get("changes") or {})
                last_exec["writes"] = 1
                if item.get("status") not in ("completed", None):
                    last_exec["is_error"] = 1
    return log


PARSERS = {"Claude Code": parse_claude, "Pi": parse_pi, "Codex": parse_codex}


# --- Structured transcript (fallback) ----------------------------------------------------------

WORKDIR = re.compile(r"workdir\W+(/[^\"'\\]+)")


def parse_transcript(transcript: dict) -> Log:
    """Build a Log from transcript.json, for sessions whose raw log is too big to fetch.
    It has messages and tool inputs but mostly no tool output, so output, is_error and
    exit codes stay NULL unless a result carries them. A user turn that follows a slash
    command before any reply is the command's expansion, so it counts as injected."""
    log = Log()
    after_command = False
    for m in transcript["sessions"][0]["messages"]:
        ts, text = m.get("timestamp"), m.get("content") if isinstance(m.get("content"), str) else ""
        if m["role"] == "user":
            tags = {t.get("tag"): t.get("content", "") for t in m.get("runtimeTags") or []}
            name = tags.get("command-name", "").lstrip("/").strip()
            if name and name not in HOUSEKEEPING_COMMANDS:
                log.add_message("user", ts, f"/{name} {tags.get('command-args', '')}".strip(), "human")
                after_command = True
            elif tags:
                log.add_message("user", ts, "\n".join(tags.values()), "injected")
            elif text.strip():
                human = not (after_command or looks_injected(text) or text.lstrip().startswith("<skill>"))
                log.add_message("user", ts, text, "human" if human else "injected")
                after_command = False
            continue
        after_command = False
        idx = log.add_message("assistant", ts, text)
        for tc in m.get("toolCalls") or []:
            args = tc.get("input")
            script = args.get("input") or args.get("patch") if isinstance(args, dict) else None
            script = script if isinstance(script, str) else ""
            name = "apply_patch" if "tools.apply_patch" in script else tc["name"]
            call = log.add_call(idx, tc["id"], name, args, script)
            result = tc.get("result") or {}
            if "output" in result:
                log.set_output(tc["id"], result["output"] or "", result.get("type") == "error")
            else:
                call["is_error"] = None
            log.cwd = log.cwd or next(iter(WORKDIR.findall(script)), "")
    return log


# --- Loading -----------------------------------------------------------------------------------

def relative(path: str, cwd: str) -> str:
    root = cwd.rstrip("/") + "/"
    if cwd and path.startswith(root):
        return path[len(root):]
    m = WORKTREE.match(path)
    if m:
        return path[m.end():]
    return path.removeprefix("./")


def parse_ts(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def session_row(session_id: str, listing: dict, meta: dict, cwd: str) -> tuple:
    get = {**meta, **listing}.get
    human = listing.get("attributionHumanAdded")
    if human is not None:
        human += listing.get("attributionHumanModified") or 0
    return (
        session_id, get("agent"), get("model"), get("displayName"), get("firstCommitAuthorUsername"),
        get("startedAt"), get("lastActivityAt"), get("checkpointCount"), get("inputTokens"),
        get("outputTokens"), get("attributionAgentLines"), human, get("prompt"), cwd,
    )


def write_log(conn, row: tuple, log: Log, checkpoints: list[dict] = (), root: str | None = None) -> None:
    """Replace one session's rows. row is the sessions row, session ID first; root is the
    directory tool-call paths are made relative to (default: the log's cwd)."""
    session_id = row[0]
    root = log.cwd if root is None else root
    cp_times = [(parse_ts(c["timestamp"]), c["checkpointId"]) for c in checkpoints]

    def checkpoint_for(ts: str | None) -> str | None:
        t = parse_ts(ts)
        return next((cid for ct, cid in cp_times if t and ct >= t), None)

    with conn:
        for table in ("sessions", "checkpoints", "messages", "tool_calls"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO sessions (session_id, agent, model, display_name, author, started_at, last_activity_at,"
            " checkpoint_count, input_tokens, output_tokens, agent_lines, human_lines, prompt, cwd)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO checkpoints (checkpoint_id, session_id, commit_sha, message, additions,"
            " deletions, files_changed, ts) VALUES (?,?,?,?,?,?,?,?)",
            [(c["checkpointId"], session_id, c.get("commitSha"), c.get("message"), c.get("additions"),
              c.get("deletions"), c.get("filesChanged"), c.get("timestamp")) for c in checkpoints],
        )
        conn.executemany(
            "INSERT INTO messages (session_id, idx, role, user_kind, ts, text, checkpoint_id) VALUES (?,?,?,?,?,?,?)",
            [(session_id, i, m["role"], m["user_kind"], m["ts"], m["text"], checkpoint_for(m["ts"]))
             for i, m in enumerate(log.messages)],
        )
        call_idx: dict[int, int] = {}
        rows = []
        for c in log.calls:
            n = call_idx[c["msg_idx"]] = call_idx.get(c["msg_idx"], -1) + 1
            files = list(dict.fromkeys(relative(f, root) for f in c["files"]))
            exit_code, is_error = exit_status(c)
            rows.append((session_id, c["msg_idx"], n, c["name"], c["input"], c["output"],
                         is_error, exit_code, json.dumps(files), c["writes"]))
        conn.executemany(
            "INSERT INTO tool_calls (session_id, msg_idx, call_idx, name, input, output, is_error, exit_code,"
            " files, writes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )


def load_session(conn, folder: Path) -> str:
    session_id = folder.name
    if not safe_session_id(session_id):
        return f"skip {session_id!r}: not a valid session ID"
    listing_path = folder / "listing.json"
    listing = json.loads(listing_path.read_text()) if listing_path.exists() else {}
    transcript = json.loads((folder / "transcript.json").read_text())
    agent = listing.get("agent") or transcript["session"].get("agent")
    parser = PARSERS.get(agent)
    if parser is None:
        return f"skip {session_id}: no parser for agent {agent!r}"

    raw = folder / "raw.jsonl"
    if raw.exists() or raw.with_name(raw.name + ".gz").exists():
        log = parser(read_jsonl(raw))
    else:  # scrape.py saves no raw log when it is over the API's size limit
        log = parse_transcript(transcript)
    checkpoints = sorted(transcript.get("checkpoints", []), key=lambda c: parse_ts(c["timestamp"]))
    write_log(conn, session_row(session_id, listing, transcript.get("session", {}), log.cwd), log, checkpoints)
    return f"loaded {session_id}  {agent:<12} {len(log.messages):>4} messages  {len(log.calls):>4} tool calls"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", nargs="*", help="Only these session IDs (default: every folder in raw/)")
    args = parser.parse_args(argv)

    # Only folders scrape.py filled: a home with local sessions alone has no raw/ at all.
    folders = [RAW_DIR / i for i in args.ids] if args.ids else sorted(
        p for p in RAW_DIR.glob("*") if (p / "transcript.json").exists())
    conn = db.connect()
    failed = []
    for folder in folders:
        try:
            print(load_session(conn, folder))
        except (ValueError, KeyError, TypeError, OSError) as e:  # one bad log shouldn't stop the rest
            failed.append(folder.name)
            print(f"failed {folder.name}: {type(e).__name__}: {e}")
    print(f"done: {len(folders) - len(failed)} loaded, {len(failed)} failed {failed or ''}")


if __name__ == "__main__":
    main()
