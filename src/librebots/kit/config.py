from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import msgspec

__all__ = ["Config", "ConfigError", "LogLevel"]

LogLevel = Literal["TRACE_HIKARI", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_UINT32_MAX = 2**32 - 1
_ShardId = Annotated[int, msgspec.Meta(ge=0, le=_UINT32_MAX)]
_ShardCount = Annotated[int, msgspec.Meta(ge=1, le=_UINT32_MAX)]

_SECRETS = frozenset({"discord_token", "hub_token"})


class ConfigError(Exception):
    """The environment does not describe a valid configuration."""


class Config(msgspec.Struct, frozen=True, kw_only=True, rename="upper"):
    """The settings of one bot process, each field read from the environment variable of the same name in upper case."""

    discord_token: str
    hub_address: str
    hub_token: str
    hub_tls: bool
    hub_ca_file: str | None = None
    shard_ids: tuple[_ShardId, ...] | None = None
    shard_count: _ShardCount | None = None
    operator_name: str
    operator_contact: str
    privacy_policy_url: str
    support_url: str | None = None
    rest_url: str | None = None
    log_level: LogLevel = "INFO"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read the configuration from `environ`, by default `os.environ`.

        `DISCORD_TOKEN` and `HUB_TOKEN` can instead be read from the file a `_FILE` variable names. Raises
        `ConfigError` on the first problem.
        """
        values = _collect(os.environ if environ is None else environ)
        try:
            return msgspec.convert(values, cls, strict=False)
        except msgspec.ValidationError as exc:
            raise ConfigError(str(exc)) from exc

    def __post_init__(self) -> None:
        if (self.shard_ids is None) != (self.shard_count is None):
            raise ValueError("Set SHARD_IDS and SHARD_COUNT together, or neither")
        if self.shard_ids is not None and self.shard_count is not None:
            if len(set(self.shard_ids)) != len(self.shard_ids):
                raise ValueError("SHARD_IDS names a shard twice")
            if max(self.shard_ids) >= self.shard_count:
                raise ValueError("Every id in SHARD_IDS must be below SHARD_COUNT")
        if self.hub_ca_file is not None and not self.hub_tls:
            raise ValueError("HUB_CA_FILE needs HUB_TLS=true")
        for name, url in (
            ("PRIVACY_POLICY_URL", self.privacy_policy_url),
            ("SUPPORT_URL", self.support_url),
            ("REST_URL", self.rest_url),
        ):
            if url is not None and not _is_http_url(url):
                raise ValueError(f"{name} must be an http or https URL with a host")

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={value!r}" for name, value in self.__rich_repr__())
        return f"{type(self).__name__}({fields})"

    def __rich_repr__(self) -> list[tuple[str, object]]:
        return [(name, "***" if name in _SECRETS else getattr(self, name)) for name in self.__struct_fields__]


def _collect(environ: Mapping[str, str]) -> dict[str, str | list[str]]:
    values: dict[str, str | list[str]] = {}
    for field in msgspec.structs.fields(Config):
        name = field.encode_name
        value = _read_secret(environ, name) if field.name in _SECRETS else _clean(environ.get(name))
        if value is None:
            continue
        if name == "SHARD_IDS":
            values[name] = [part.strip() for part in value.split(",")]
        elif name == "LOG_LEVEL":
            values[name] = value.upper()
        else:
            values[name] = value
    return values


def _read_secret(environ: Mapping[str, str], name: str) -> str | None:
    value = _clean(environ.get(name))
    path = _clean(environ.get(f"{name}_FILE"))
    if path is None:
        return value
    if value is not None:
        raise ConfigError(f"Set {name} or {name}_FILE, not both")
    try:
        content = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read {name}_FILE: {exc}") from exc
    secret = _clean(content)
    if secret is None:
        raise ConfigError(f"{name}_FILE names an empty file")
    return secret


def _is_http_url(url: str) -> bool:
    if any(char.isspace() for char in url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme in {"http", "https"} and bool(parts.hostname) and (parts.port is None or parts.port > 0)
    except ValueError:
        return False


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None
