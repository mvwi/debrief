import pytest

from debrief import config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    for name in set(config.ENV.values()):
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
