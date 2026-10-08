import pytest
from dotenv import load_dotenv as real_load_dotenv

import deck_mcp.config as config_mod
from deck_mcp.config import ConfigError, load_config

ALL_VARS = (
    "NEXTCLOUD_URL",
    "NEXTCLOUD_USERNAME",
    "NEXTCLOUD_APP_PASSWORD",
    "DECK_BOARD",
    "DECK_LANE",
    "DECK_BOARD_ID",
    "DECK_LANE_ID",
    "DECK_ASSIGNEE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)

    # Neutralise dotenv's directory walk so a developer's real .env never
    # leaks into the tests; an explicitly passed env file still works.
    def _restricted(env_file=None, **kwargs):
        if env_file is not None:
            return real_load_dotenv(env_file, **kwargs)
        return False

    monkeypatch.setattr(config_mod, "load_dotenv", _restricted)


def _set_all(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://cloud.example.com/")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "ai_agent")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "secret")
    monkeypatch.setenv("DECK_BOARD", "CMA")
    monkeypatch.setenv("DECK_LANE", "AI Agent")


def test_load_config_ok(monkeypatch):
    _set_all(monkeypatch)
    cfg = load_config()
    assert cfg.url == "https://cloud.example.com"
    assert cfg.username == "ai_agent"
    assert cfg.board == "CMA"
    assert cfg.lane == "AI Agent"
    assert cfg.board_id is None
    assert cfg.agent_uid == "ai_agent"


def test_missing_credentials(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://cloud.example.com")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "NEXTCLOUD_USERNAME" in str(exc.value)
    assert "NEXTCLOUD_APP_PASSWORD" in str(exc.value)


def test_missing_board_and_lane(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_URL", "https://x.example")
    monkeypatch.setenv("NEXTCLOUD_USERNAME", "u")
    monkeypatch.setenv("NEXTCLOUD_APP_PASSWORD", "p")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "DECK_BOARD" in str(exc.value)


def test_lane_required_even_with_board_id(monkeypatch):
    _set_all(monkeypatch)
    monkeypatch.delenv("DECK_LANE")
    monkeypatch.setenv("DECK_BOARD_ID", "11")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "DECK_LANE" in str(exc.value)


def test_numeric_ids_and_assignee(monkeypatch):
    _set_all(monkeypatch)
    monkeypatch.setenv("DECK_BOARD_ID", "11")
    monkeypatch.setenv("DECK_LANE_ID", "49")
    monkeypatch.setenv("DECK_ASSIGNEE", "helper")
    cfg = load_config()
    assert cfg.board_id == 11
    assert cfg.lane_id == 49
    assert cfg.agent_uid == "helper"


def test_invalid_board_id(monkeypatch):
    _set_all(monkeypatch)
    monkeypatch.setenv("DECK_BOARD_ID", "not-a-number")
    with pytest.raises(ConfigError) as exc:
        load_config()
    assert "DECK_BOARD_ID" in str(exc.value)


def test_bad_url_scheme(monkeypatch):
    _set_all(monkeypatch)
    monkeypatch.setenv("NEXTCLOUD_URL", "ftp://cloud.example.com")
    with pytest.raises(ConfigError):
        load_config()


def test_env_file_loading(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "NEXTCLOUD_URL=https://file.example\n"
        "NEXTCLOUD_USERNAME=fileuser\n"
        "NEXTCLOUD_APP_PASSWORD=filepass\n"
        "DECK_BOARD=BoardX\n"
        "DECK_LANE=LaneY\n"
    )
    cfg = load_config(str(env_file))
    assert cfg.url == "https://file.example"
    assert cfg.board == "BoardX"
    assert cfg.lane == "LaneY"
