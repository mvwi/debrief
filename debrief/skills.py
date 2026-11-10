"""Find skill and slash-command uses in normalized sessions.

One row per skill or slash-command use:
  human: a typed "/command" (Claude Code, Codex, Pi) or a Codex "<skill>" block
         injected after a "$skill" mention.
  agent: a Claude Code Skill tool call, or a Codex/Pi tool call that reads a
         SKILL.md file (Claude Code's own SKILL.md reads are repo work, not uses).
"""

import json
import re
import sqlite3

# Built-in CLI commands that take no skill or prompt file; not counted as skill uses.
BUILTIN_COMMANDS = {
    "add-dir", "agents", "bashes", "clear", "compact", "config", "context", "cost", "doctor", "exit", "export",
    "help", "hooks", "ide", "login", "logout", "mcp", "memory", "model", "permissions", "plugin", "resume",
    "rewind", "status", "statusline", "terminal-setup", "todos", "usage", "vim",
}
SLASH_RE = re.compile(r"/([A-Za-z][\w:.-]*)(?=\s|$)")  # typed command; "/var/x" fails the lookahead
SKILL_BLOCK_RE = re.compile(r"\s*<skill>\s*<name>([^<]+)</name>")  # Codex expansion of a "$skill" mention
SKILL_PATH_RE = re.compile(r"[\w.~/-]+/SKILL\.md")
NOT_A_READ_RE = re.compile(r"\b(?:ls|wc|find|stat)\s[^'\"\n;|&]*$")  # listing a skill file is not using it
COLUMNS = ["session_id", "msg_idx", "skill", "invoked_by", "agent", "model", "ts"]


def skill_from_path(path: str) -> str | None:
    """Name a skill from its SKILL.md path, keeping the plugin namespace.

    plugins/cache/skills/entire/0.7.0/skills/review/SKILL.md  -> entire:review
    .codex/superpowers/skills/brainstorming/SKILL.md          -> superpowers:brainstorming
    .agents/skills/superpowers/brainstorming/SKILL.md         -> superpowers:brainstorming
    .agents/skills/pfleidi/skills/pr/SKILL.md                 -> pfleidi:pr
    .agents/skills/test-auditor/SKILL.md                      -> test-auditor
    """
    parts = path.split("/")
    if len(parts) < 2 or not re.fullmatch(r"[a-z0-9][\w.-]*", parts[-2]):
        return None
    name, parent = parts[-2], parts[-3] if len(parts) > 2 else ""
    if parent != "skills":
        grand = parts[-4] if len(parts) > 3 else ""
        return f"{parent}:{name}" if grand == "skills" and not parent.startswith(".") else name
    owners = [p for p in parts[:-3] if p and not re.match(r"\d", p)]  # skip version dirs
    owner = owners[-1] if owners else ""
    nested = len(parts) > 4 and parts[-5] == "skills"
    if owner == "superpowers" or nested or ("plugins" in parts and owner != "cache"):
        return f"{owner}:{name}"
    return name


def skills_read(tool_input) -> list[str]:
    text = str(tool_input or "")
    found = (
        skill_from_path(m.group())
        for m in SKILL_PATH_RE.finditer(text)
        if not NOT_A_READ_RE.search(text[max(0, m.start() - 60) : m.start()])
    )
    return [s for s in dict.fromkeys(found) if s]


def slash_skill(text) -> str | None:
    m = SLASH_RE.match(str(text or ""))
    if not m:
        return None
    name = m.group(1).removeprefix("skill:")  # Pi spells skills "/skill:name"
    return None if name in BUILTIN_COMMANDS else name


def _skill_arg(raw) -> str:
    try:
        val = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError):
        return ""
    return str(val.get("skill") or "").lstrip("/") if isinstance(val, dict) else ""


def uses(conn: sqlite3.Connection) -> list[dict]:
    """Every skill use, oldest first; one row per skill per message."""
    msgs = conn.execute("""
        SELECT m.session_id, m.idx AS msg_idx, m.user_kind, m.text, m.ts, s.agent, s.model
        FROM messages m JOIN sessions s ON s.session_id = m.session_id
        WHERE m.role = 'user' AND (m.text LIKE '/%' OR m.text LIKE '%<skill>%')
    """).fetchall()
    calls = conn.execute("""
        SELECT c.session_id, c.msg_idx, c.name, c.input, m.ts, s.agent, s.model
        FROM tool_calls c
        JOIN sessions s ON s.session_id = c.session_id
        LEFT JOIN messages m ON m.session_id = c.session_id AND m.idx = c.msg_idx
        WHERE (s.agent = 'Claude Code' AND c.name = 'Skill')
           OR (s.agent != 'Claude Code' AND c.input LIKE '%SKILL.md%' AND COALESCE(c.writes, 0) = 0
               AND c.name NOT IN ('apply_patch', 'edit', 'write'))
    """).fetchall()
    rows = []
    for r in msgs:
        block = SKILL_BLOCK_RE.match(str(r[3] or ""))
        skill = block.group(1).strip() if block else (slash_skill(r[3]) if r[2] == "human" else None)
        if skill:
            rows.append((r[0], r[1], skill, "human", r[5], r[6], r[4]))
    for r in calls:
        found = [_skill_arg(r[3])] if r[2] == "Skill" else skills_read(r[3])
        rows += [(r[0], r[1], s, "agent", r[5], r[6], r[4]) for s in found if s]
    seen, out = set(), []
    for row in sorted(rows, key=lambda x: (x[6] or "", x[0], x[1])):
        if (key := row[:3]) not in seen:
            seen.add(key)
            out.append(dict(zip(COLUMNS, row, strict=True)))
    return out
