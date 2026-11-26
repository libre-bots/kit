from pathlib import Path

import pytest

from librebots.kit.config import Config, ConfigError

REQUIRED = {
    "DISCORD_TOKEN": "discord-token",
    "HUB_ADDRESS": "hub:50051",
    "HUB_TOKEN": "hub-token",
    "HUB_TLS": "false",
    "OPERATOR_NAME": "Example Operator",
    "OPERATOR_CONTACT": "ops@example.org",
    "PRIVACY_POLICY_URL": "https://example.org/privacy",
}


def env(**values: str) -> dict[str, str]:
    return {**REQUIRED, **values}


def without(name: str) -> dict[str, str]:
    return {key: value for key, value in REQUIRED.items() if key != name}


def test_reads_the_required_values_and_fills_in_defaults() -> None:
    config = Config.from_env(env())

    assert config.discord_token == "discord-token"
    assert config.hub_address == "hub:50051"
    assert config.hub_token == "hub-token"
    assert config.hub_tls is False
    assert config.hub_ca_file is None
    assert config.shard_ids is None
    assert config.shard_count is None
    assert config.operator_name == "Example Operator"
    assert config.operator_contact == "ops@example.org"
    assert config.privacy_policy_url == "https://example.org/privacy"
    assert config.support_url is None
    assert config.rest_url is None
    assert config.log_level == "INFO"


def test_reads_the_optional_values() -> None:
    config = Config.from_env(
        env(
            HUB_TLS="true",
            HUB_CA_FILE="/run/secrets/hub_ca.pem",
            SUPPORT_URL="https://example.org/support",
            REST_URL="http://rest-proxy:8080/api/v10",
            LOG_LEVEL="DEBUG",
        )
    )

    assert config.hub_ca_file == "/run/secrets/hub_ca.pem"
    assert config.support_url == "https://example.org/support"
    assert config.rest_url == "http://rest-proxy:8080/api/v10"
    assert config.log_level == "DEBUG"


def test_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)

    assert Config.from_env().discord_token == "discord-token"


def test_ignores_unrelated_variables() -> None:
    assert Config.from_env(env(PATH="/usr/bin", HOME="/root")).hub_address == "hub:50051"


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_requires_each_required_value(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Config.from_env(without(name))


@pytest.mark.parametrize("value", ["", "  ", "\n"])
def test_treats_a_blank_value_as_missing(value: str) -> None:
    with pytest.raises(ConfigError, match="OPERATOR_NAME"):
        Config.from_env(env(OPERATOR_NAME=value))


def test_strips_surrounding_whitespace() -> None:
    assert Config.from_env(env(HUB_ADDRESS="  hub:50051\n")).hub_address == "hub:50051"


@pytest.mark.parametrize("name", ["DISCORD_TOKEN", "HUB_TOKEN"])
def test_reads_a_secret_from_its_file(tmp_path: Path, name: str) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-file\n", encoding="utf-8")
    values = without(name) | {f"{name}_FILE": str(secret)}

    assert getattr(Config.from_env(values), name.lower()) == "from-file"


def test_rejects_a_secret_set_directly_and_as_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-file", encoding="utf-8")

    with pytest.raises(ConfigError, match="DISCORD_TOKEN_FILE"):
        Config.from_env(env(DISCORD_TOKEN_FILE=str(secret)))


def test_rejects_a_missing_secret_file(tmp_path: Path) -> None:
    values = without("HUB_TOKEN") | {"HUB_TOKEN_FILE": str(tmp_path / "missing")}

    with pytest.raises(ConfigError, match="HUB_TOKEN_FILE"):
        Config.from_env(values)


def test_rejects_an_empty_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("\n", encoding="utf-8")
    values = without("HUB_TOKEN") | {"HUB_TOKEN_FILE": str(secret)}

    with pytest.raises(ConfigError, match="HUB_TOKEN_FILE"):
        Config.from_env(values)


def test_only_secrets_have_a_file_variant(tmp_path: Path) -> None:
    address = tmp_path / "address"
    address.write_text("hub:50051", encoding="utf-8")
    values = without("HUB_ADDRESS") | {"HUB_ADDRESS_FILE": str(address)}

    with pytest.raises(ConfigError, match="HUB_ADDRESS"):
        Config.from_env(values)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("false", False), ("False", False), ("0", False)],
)
def test_parses_hub_tls(value: str, expected: bool) -> None:
    assert Config.from_env(env(HUB_TLS=value)).hub_tls is expected


@pytest.mark.parametrize("value", ["yes", "on", "2"])
def test_rejects_an_unclear_hub_tls(value: str) -> None:
    with pytest.raises(ConfigError, match="HUB_TLS"):
        Config.from_env(env(HUB_TLS=value))


def test_rejects_a_ca_file_without_tls() -> None:
    with pytest.raises(ConfigError, match="HUB_CA_FILE"):
        Config.from_env(env(HUB_CA_FILE="/run/secrets/hub_ca.pem"))


def test_parses_shards() -> None:
    config = Config.from_env(env(SHARD_IDS="0, 2,3", SHARD_COUNT="4"))

    assert config.shard_ids == (0, 2, 3)
    assert config.shard_count == 4


@pytest.mark.parametrize(
    "values",
    [{"SHARD_IDS": "0"}, {"SHARD_COUNT": "2"}],
    ids=["ids without count", "count without ids"],
)
def test_requires_shard_ids_and_count_together(values: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="SHARD_IDS and SHARD_COUNT"):
        Config.from_env(env(**values))


@pytest.mark.parametrize(
    ("ids", "count"),
    [
        ("0,2", "2"),
        ("-1", "2"),
        ("0,,1", "2"),
        ("a", "2"),
        ("0,0", "2"),
        ("0", "0"),
        ("0", "x"),
    ],
    ids=["id too high", "negative id", "empty id", "not a number", "duplicate id", "count zero", "count not a number"],
)
def test_rejects_invalid_shards(ids: str, count: str) -> None:
    with pytest.raises(ConfigError, match="SHARD_"):
        Config.from_env(env(SHARD_IDS=ids, SHARD_COUNT=count))


@pytest.mark.parametrize("name", ["PRIVACY_POLICY_URL", "SUPPORT_URL", "REST_URL"])
@pytest.mark.parametrize("value", ["example.org/privacy", "ftp://example.org", "https://"])
def test_rejects_a_url_without_http(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        Config.from_env(env(**{name: value}))


def test_accepts_a_lowercase_log_level() -> None:
    assert Config.from_env(env(LOG_LEVEL="warning")).log_level == "WARNING"


def test_accepts_hikaris_trace_level() -> None:
    assert Config.from_env(env(LOG_LEVEL="trace_hikari")).log_level == "TRACE_HIKARI"


def test_rejects_an_unknown_log_level() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Config.from_env(env(LOG_LEVEL="LOUD"))


def test_keeps_the_tokens_out_of_its_representations() -> None:
    config = Config.from_env(env())
    shown = [repr(config), str(config), f"{config}", repr(config.__rich_repr__())]

    for text in shown:
        assert "discord-token" not in text
        assert "hub-token" not in text
        assert "hub:50051" in text
