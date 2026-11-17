"""Which model runs each stage.

Set in DEBRIEF_HOME/config.toml, one table per stage:

    [label]
    llm = "openrouter"            # or "claude-code"
    model = "typesafe/jev-1.13"   # optional; each llm has a default

`[github] enabled = false` stops sync from asking GitHub about your sessions' pull requests.

`[projects] include = ["/abs/path", ...]` picks the projects to analyse: only their sessions
are labeled, turned into findings and ideas, and shown in the report. With none picked, sync
stops after ingest. `debrief sync --project DIR` adds one; `debrief config --drop DIR` removes one.

A flag beats an environment variable, which beats config.toml, which beats DEFAULTS.

The Settings page of `debrief open` writes config.toml, and keeps an OpenRouter key in
DEBRIEF_HOME/.env.
"""

import argparse
import json
import os
import re
import sys
import tomllib

from dotenv import dotenv_values, find_dotenv, load_dotenv, set_key, unset_key

from debrief.paths import HOME, NAME

CONFIG_PATH = HOME / "config.toml"
ENV_PATH = HOME / ".env"
KEY = "OPENROUTER_API_KEY"
# Both are written into files unquoted or between plain quotes, so neither may hold a quote,
# a backslash or a line break.
MODEL_RE = re.compile(r"[\w./:@-]{1,100}", re.ASCII)
KEY_RE = re.compile(r"[\w.-]{20,200}", re.ASCII)

MAX_PROJECTS, MAX_PATH = 200, 500

# stage -> llm -> default model. The first llm listed is the stage's default.
DEFAULTS = {
    "label": {"claude-code": "claude-haiku-5-5", "openrouter": "typesafe/jev-1.13"},
    "synthesize": {"claude-code": "claude-opus-5-5", "openrouter": "anthropic/claude-opus-5.5"},
    "recommend": {"claude-code": "claude-opus-5-5"},
    "verify": {"claude-code": "claude-sonnet-5"},
}
ENV = {
    ("label", "openrouter"): "JEV_MODEL",
    ("synthesize", "openrouter"): "SYNTH_MODEL",
    ("synthesize", "claude-code"): "CLAUDE_CODE_MODEL",
    ("recommend", "claude-code"): "CLAUDE_CODE_MODEL",
    ("verify", "claude-code"): "VERIFY_MODEL",
}


def check(cfg: dict) -> None:
    """Raise ValueError unless cfg is what config.toml may hold."""
    for stage, table in cfg.items():
        if stage == "github" and isinstance(table, dict):
            continue
        if stage == "projects" and isinstance(table, dict):
            include = table.get("include", [])
            if not isinstance(include, list) or len(include) > MAX_PROJECTS or not all(
                    isinstance(p, str) and os.path.isabs(p) and len(p) <= MAX_PATH and p.isprintable() for p in include):
                raise ValueError(f"[projects] include must be a list of at most {MAX_PROJECTS} absolute paths, "
                                 f"each at most {MAX_PATH} characters with no control characters")
            continue
        if stage not in DEFAULTS or not isinstance(table, dict):
            raise ValueError(f"unknown stage [{stage}]. Stages: {', '.join(DEFAULTS)}")
        llm, model = table.get("llm", next(iter(DEFAULTS[stage]))), table.get("model")
        if not isinstance(llm, str) or llm not in DEFAULTS[stage]:
            raise ValueError(f"[{stage}] llm must be one of: {', '.join(DEFAULTS[stage])}")
        if model and not (isinstance(model, str) and MODEL_RE.fullmatch(model)):
            raise ValueError(f"[{stage}] model must be 1 to 100 letters, digits or . / : @ - _")


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text())
        check(cfg)
    except ValueError as e:  # TOMLDecodeError is one
        sys.exit(f"debrief: {CONFIG_PATH}: {e}")
    return cfg


def save(cfg: dict) -> None:
    """Write cfg as config.toml. Comments in the old file are lost."""
    check(cfg)
    lines = []
    for stage in DEFAULTS:
        rows = [f'{k} = "{v}"' for k in ("llm", "model") if (v := cfg.get(stage, {}).get(k))]
        if rows:
            lines += [f"[{stage}]", *rows, ""]
    if "github" in cfg:
        lines += ["[github]", f"enabled = {'false' if cfg['github'].get('enabled') is False else 'true'}", ""]
    if include := cfg.get("projects", {}).get("include"):
        # A JSON list of strings is a TOML one too: both escape a quote and a backslash the same
        # way, and check() has ruled out control characters.
        lines += ["[projects]", f"include = {json.dumps(include, ensure_ascii=False)}", ""]
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(CONFIG_PATH)


def load_env() -> None:
    """Read .env files into the environment: the one found from here up, then DEBRIEF_HOME/.env.

    Neither replaces a variable that is already set, so the shell wins, then the first file.
    """
    load_dotenv()
    load_dotenv(ENV_PATH)


def save_key(key) -> None:
    """Keep the OpenRouter key in DEBRIEF_HOME/.env, readable only by its owner."""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError("a key is 20 to 200 letters, digits or . - _")
    HOME.mkdir(parents=True, exist_ok=True)
    os.close(os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT, 0o600))
    os.chmod(ENV_PATH, 0o600)
    set_key(ENV_PATH, KEY, key, quote_mode="never")
    os.chmod(ENV_PATH, 0o600)


def remove_key() -> None:
    if ENV_PATH.exists() and KEY in dotenv_values(ENV_PATH):
        unset_key(ENV_PATH, KEY)
        os.chmod(ENV_PATH, 0o600)


def settings() -> dict:
    """What the Settings page shows. It says whether a key is set and how it ends, never the key."""
    cfg = load()
    here, home = dotenv_values(find_dotenv()), dotenv_values(ENV_PATH)
    env = {**home, **here, **os.environ}  # what a stage sees after load_env()
    shell_key = os.environ.get(KEY) or here.get(KEY)
    key = shell_key or home.get(KEY) or ""
    return {
        "stages": {stage: {"llm": cfg.get(stage, {}).get("llm") or next(iter(llms)),
                           "model": cfg.get(stage, {}).get("model") or "",
                           "defaults": llms,
                           # llm -> the environment variable that is set and beats `model`
                           "overrides": {llm: ENV[stage, llm] for llm in llms if env.get(ENV.get((stage, llm)))}}
                   for stage, llms in DEFAULTS.items()},
        "github": cfg.get("github", {}).get("enabled", True) is not False,
        "projects": cfg.get("projects", {}).get("include", []),
        "key": {"set": bool(key), "last4": key[-4:] if len(key) >= 20 else "", "source": "env" if shell_key else "file"},
    }


def choose(stage: str, llm: str | None = None, model: str | None = None) -> tuple[str, str]:
    """The (llm, model) a stage should use. `llm` and `model` are the command-line flags, if given."""
    cfg = load().get(stage, {})
    cfg_llm = cfg.get("llm") or next(iter(DEFAULTS[stage]))
    llm = llm or cfg_llm
    # A model set in config.toml belongs to the llm set beside it, not to one picked by flag.
    cfg_model = cfg.get("model") if llm == cfg_llm else None
    model = model or os.environ.get(ENV.get((stage, llm), "")) or cfg_model or DEFAULTS[stage][llm]
    return llm, model


def github_enabled() -> bool:
    """False only when config.toml says `[github] enabled = false`."""
    return load().get("github", {}).get("enabled", True) is not False


def projects() -> list[str]:
    """The picked projects: the folders `[projects] include` lists. Empty when none is picked."""
    return load().get("projects", {}).get("include", [])


def pick(project: str) -> None:
    """Add a folder to the picked projects."""
    cfg = load()
    if project not in projects():
        save({**cfg, "projects": {"include": [*projects(), project]}})


def drop(project: str) -> bool:
    """Take a folder out of the picked projects. False when it was not picked."""
    cfg, was = load(), projects()
    if project in was:
        save({**cfg, "projects": {"include": [p for p in was if p != project]}})
    return project in was


def main(argv: list[str] | None = None) -> None:
    """Print the model each stage will use and the picked projects, after dropping any named."""
    p = argparse.ArgumentParser(prog=f"{NAME} config", description=main.__doc__)
    p.add_argument("--drop", metavar="DIR", action="append", default=[],
                   help="Stop analysing this project (repeatable). Its findings stay in the database; the report hides them")
    for d in p.parse_args(argv).drop:
        d = os.path.abspath(os.path.expanduser(d))
        print(f"dropped {d}" if drop(d) else f"{d} is not picked, so there is nothing to drop")
    print(f"{CONFIG_PATH}{'' if CONFIG_PATH.exists() else ' (not there; using defaults)'}")
    for stage in DEFAULTS:
        llm, model = choose(stage)
        print(f"  {stage:<11}{llm:<13}{model}")
    print(f"  projects   {', '.join(projects()) or 'none picked: `debrief sync` lists them'}")
