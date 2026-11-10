"""Which model runs each stage.

Set in DEBRIEF_HOME/config.toml, one table per stage:

    [label]
    llm = "openrouter"            # or "claude-code"
    model = "typesafe/jev-1.13"   # optional; each llm has a default

A flag beats an environment variable, which beats config.toml, which beats DEFAULTS.
"""

import os
import sys
import tomllib

from debrief.paths import HOME

CONFIG_PATH = HOME / "config.toml"

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


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"debrief: {CONFIG_PATH} is not valid TOML: {e}")
    for stage, table in cfg.items():
        if stage not in DEFAULTS or not isinstance(table, dict):
            sys.exit(f"debrief: {CONFIG_PATH}: unknown stage [{stage}]. Stages: {', '.join(DEFAULTS)}")
        if table.get("llm", next(iter(DEFAULTS[stage]))) not in DEFAULTS[stage]:
            sys.exit(f"debrief: {CONFIG_PATH}: [{stage}] llm must be one of: {', '.join(DEFAULTS[stage])}")
    return cfg


def choose(stage: str, llm: str | None = None, model: str | None = None) -> tuple[str, str]:
    """The (llm, model) a stage should use. `llm` and `model` are the command-line flags, if given."""
    cfg = load().get(stage, {})
    cfg_llm = cfg.get("llm") or next(iter(DEFAULTS[stage]))
    llm = llm or cfg_llm
    # A model set in config.toml belongs to the llm set beside it, not to one picked by flag.
    cfg_model = cfg.get("model") if llm == cfg_llm else None
    model = model or os.environ.get(ENV.get((stage, llm), "")) or cfg_model or DEFAULTS[stage][llm]
    return llm, model


def main(argv: list[str] | None = None) -> None:
    """Print the model each stage will use."""
    print(f"{CONFIG_PATH}{'' if CONFIG_PATH.exists() else ' (not there; using defaults)'}")
    for stage in DEFAULTS:
        llm, model = choose(stage)
        print(f"  {stage:<11}{llm:<13}{model}")
