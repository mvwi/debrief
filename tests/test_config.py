import os
import stat

import pytest

from debrief import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "find_dotenv", lambda: str(tmp_path / "no-such.env"))
    for name in {*config.ENV.values(), config.KEY}:
        monkeypatch.delenv(name, raising=False)
    return path


def test_defaults_without_a_file(cfg):
    assert config.choose("label") == ("claude-code", "claude-haiku-5-5")
    assert config.choose("verify") == ("claude-code", "claude-sonnet-5")


def test_file_sets_llm_and_model(cfg):
    cfg.write_text('[label]\nllm = "openrouter"\n[synthesize]\nmodel = "claude-sonnet-5"\n')
    assert config.choose("label") == ("openrouter", "typesafe/jev-1.13")
    assert config.choose("synthesize") == ("claude-code", "claude-sonnet-5")


def test_flag_beats_env_beats_file(cfg, monkeypatch):
    cfg.write_text('[label]\nllm = "openrouter"\nmodel = "from-file"\n')
    assert config.choose("label") == ("openrouter", "from-file")
    monkeypatch.setenv("JEV_MODEL", "from-env")
    assert config.choose("label") == ("openrouter", "from-env")
    assert config.choose("label", model="from-flag") == ("openrouter", "from-flag")
    # The file's model goes with the file's llm, not with one picked by flag.
    assert config.choose("label", llm="claude-code") == ("claude-code", "claude-haiku-5-5")


@pytest.mark.parametrize("text", ['[lable]\nllm = "openrouter"\n', '[recommend]\nllm = "openrouter"\n', "[label\n"])
def test_bad_file_exits_with_a_message(cfg, text):
    cfg.write_text(text)
    with pytest.raises(SystemExit, match="config.toml"):
        config.choose("label")


def test_github_is_on_unless_the_file_says_no(cfg):
    assert config.github_enabled()
    cfg.write_text("[github]\nenabled = false\n")
    assert not config.github_enabled()
    assert config.choose("label") == ("claude-code", "claude-haiku-5-5")


FAKE_KEY = "sk-or-v1-" + "0" * 28 + "a1b2"


def test_save_then_load_round_trips(cfg):
    saved = {"label": {"llm": "openrouter", "model": "typesafe/jev-1.13"}, "synthesize": {"llm": "claude-code"},
             "github": {"enabled": False}}
    config.save(saved)
    assert config.load() == saved
    assert config.choose("label") == ("openrouter", "typesafe/jev-1.13")
    assert not config.github_enabled()
    config.save({"github": {"enabled": True}})
    assert config.load() == {"github": {"enabled": True}}


@pytest.mark.parametrize("bad", [
    {"lable": {"llm": "openrouter"}},
    {"recommend": {"llm": "openrouter"}},
    {"label": {"llm": ["openrouter"]}},
    {"label": {"model": 'x"\nllm = "y'}},
    {"label": {"model": "m" * 101}},
    {"label": {"model": 5}},
    {"label": "openrouter"},
])
def test_save_refuses_what_load_would(cfg, bad):
    with pytest.raises(ValueError):
        config.save(bad)
    assert not cfg.exists()


def test_key_file_is_private_and_keeps_other_lines(cfg):
    config.ENV_PATH.write_text("JEV_MODEL=other/model\n")
    os.chmod(config.ENV_PATH, 0o644)
    config.save_key(FAKE_KEY)
    assert stat.S_IMODE(config.ENV_PATH.stat().st_mode) == 0o600
    assert config.ENV_PATH.read_text() == f"JEV_MODEL=other/model\nOPENROUTER_API_KEY={FAKE_KEY}\n"
    assert config.settings()["key"] == {"set": True, "last4": "a1b2", "source": "file"}
    assert config.settings()["stages"]["label"]["overrides"] == {"openrouter": "JEV_MODEL"}
    config.remove_key()
    assert config.ENV_PATH.read_text() == "JEV_MODEL=other/model\n"
    assert config.settings()["key"]["set"] is False


@pytest.mark.parametrize("bad", ["short", "sk-or-v1-" + "0" * 30 + "\nEVIL=1", "k" * 201, "has space " * 3, None, 5])
def test_bad_key_is_refused(cfg, bad):
    with pytest.raises(ValueError):
        config.save_key(bad)
    assert not config.ENV_PATH.exists()


def test_environment_beats_the_key_file(cfg, monkeypatch):
    config.save_key(FAKE_KEY)
    monkeypatch.setenv(config.KEY, "sk-or-v1-" + "1" * 28 + "zzzz")
    assert config.settings()["key"] == {"set": True, "last4": "zzzz", "source": "env"}
    config.load_env()
    assert os.environ[config.KEY].endswith("zzzz")


def test_load_env_reads_the_key_file(cfg, monkeypatch):
    config.save_key(FAKE_KEY)
    monkeypatch.setattr(config, "load_dotenv", lambda path=None: path and config.os.environ.update(config.dotenv_values(path)))
    config.load_env()
    assert os.environ.pop(config.KEY) == FAKE_KEY


def test_picked_projects_round_trip_and_survive_other_saves(cfg):
    assert config.projects() == []
    config.save({"label": {"llm": "openrouter"}})
    odd = ['/home/me/a "quoted" name', "/home/me/back\\slash", "/home/me/caf\u00e9 \u65e5\u672c", "/home/me/alpha"]
    for path in [*odd, "/home/me/alpha"]:  # the last is already picked
        config.pick(path)
    assert config.projects() == odd == config.settings()["projects"]
    assert config.choose("label")[0] == "openrouter"
    bad = ["/a", ["relative/path"], [1], ["/a\nb"], ["/a\x7fb"], ["/" + "a" * 500], ["/a"] * 201]
    for include in bad:
        with pytest.raises(ValueError, match="absolute paths"):
            config.save({"projects": {"include": include}})
    assert config.projects() == odd
    config.save({"projects": {"include": []}})  # nothing picked: the table is not written
    assert config.projects() == [] and "projects" not in cfg.read_text()


def test_drop_takes_a_picked_project_out_and_keeps_the_rest(cfg, monkeypatch, capsys):
    cfg.write_text('[label]\nllm = "openrouter"\n[github]\nenabled = false\n')
    monkeypatch.setenv("HOME", "/home/me")
    for p in ("/home/me/alpha", "/home/me/beta", "/home/me/gamma"):
        config.pick(p)
    config.main(["--drop", "~/alpha", "--drop", "/home/me/x/../gamma/"])
    out = capsys.readouterr().out
    assert "dropped /home/me/alpha" in out and "dropped /home/me/gamma" in out
    assert config.projects() == ["/home/me/beta"]
    assert config.choose("label")[0] == "openrouter" and not config.github_enabled()
    assert out.splitlines()[-1].split() == ["projects", "/home/me/beta"]


def test_drop_of_a_project_that_is_not_picked_says_so_and_changes_nothing(cfg, capsys):
    config.main(["--drop", "/home/me/alpha"])  # no config.toml at all
    assert "/home/me/alpha is not picked" in capsys.readouterr().out and not cfg.exists()
    config.pick("/home/me/beta")
    before = cfg.read_text()
    config.main(["--drop", "/home/me/alpha"])  # returns, so the exit code is 0
    assert "/home/me/alpha is not picked" in capsys.readouterr().out and cfg.read_text() == before
    config.main(["--drop", "/home/me/beta"])  # the last one
    assert config.projects() == [] and "none picked" in capsys.readouterr().out
