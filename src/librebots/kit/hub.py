from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Protocol

import grpc
import grpc.aio
import msgspec
from librebots.hub import connect
from librebots.hub.v1.guilds_pb2_grpc import GuildServiceStub
from librebots.hub.v1.info_pb2 import GetInfoRequest
from librebots.hub.v1.info_pb2_grpc import InfoServiceStub
from librebots.hub.v1.privacy_pb2_grpc import PrivacyServiceStub
from librebots.hub.v1.stats_pb2_grpc import StatsServiceStub

from librebots.kit.config import Config

__all__ = ["Hub", "HubError", "ShardedApp", "connect_hub"]

_LOGGER = logging.getLogger(__name__)
_VERSION = re.compile(r"(\d+)\.(\d+)\.\d+")


class HubError(Exception):
    """The hub could not be reached, rejected the bot or is too old for it."""


class ShardedApp(Protocol):
    """Anything with hikari's `shards` and `shard_count`, such as a `hikari.GatewayBot`."""

    @property
    def shards(self) -> Mapping[int, object]: ...

    @property
    def shard_count(self) -> int: ...


class Hub(msgspec.Struct, frozen=True, kw_only=True):
    """A connection to the hub with the stubs of the services every bot uses.

    A bot's own services take `channel`, for example `StarboardServiceStub(hub.channel)`.
    """

    channel: grpc.aio.Channel
    version: str
    guilds: GuildServiceStub
    privacy: PrivacyServiceStub
    stats: StatsServiceStub

    async def close(self) -> None:
        """Close the connection and cancel the calls still running on it."""
        await self.channel.close()


async def connect_hub(config: Config, *, shards: ShardedApp | None = None, timeout: float = 60) -> Hub:
    """Connect to the hub and return once it has answered.

    Every call names the shards of this process: those `shards` runs at that moment, or else `SHARD_IDS` and
    `SHARD_COUNT` from the configuration. Waits up to `timeout` seconds for the hub to come up. Raises `HubError` if
    it does not, if it rejects `HUB_TOKEN`, or if it is older than the installed `librebots-hub` client.
    """
    channel = connect(
        config.hub_address,
        config.hub_token,
        tls=config.hub_tls,
        root_certificates=await _read_ca_file(config.hub_ca_file),
        shards=_shard_label(config, shards),
    )
    try:
        hub_version = await _hub_version(channel, config.hub_address, timeout)
        _check_version(hub_version, await asyncio.to_thread(installed_version, "librebots-hub"))
    except BaseException:
        await channel.close()
        raise
    _LOGGER.info("Connected to the hub at %s, version %s", config.hub_address, hub_version)
    return Hub(
        channel=channel,
        version=hub_version,
        guilds=GuildServiceStub(channel),
        privacy=PrivacyServiceStub(channel),
        stats=StatsServiceStub(channel),
    )


async def _read_ca_file(path: str | None) -> bytes | None:
    if path is None:
        return None
    try:
        return await asyncio.to_thread(Path(path).read_bytes)
    except OSError as exc:
        raise HubError(f"Cannot read HUB_CA_FILE: {exc}") from exc


def _shard_label(config: Config, app: ShardedApp | None) -> Callable[[], tuple[Sequence[int], int] | None] | None:
    if app is not None:

        def label() -> tuple[Sequence[int], int] | None:
            if not app.shards:
                return None
            return tuple(app.shards), app.shard_count

        return label
    if config.shard_ids is not None and config.shard_count is not None:
        configured = (config.shard_ids, config.shard_count)
        return lambda: configured
    return None


async def _hub_version(channel: grpc.aio.Channel, address: str, timeout: float) -> str:
    try:
        response = await InfoServiceStub(channel).GetInfo(GetInfoRequest(), timeout=timeout, wait_for_ready=True)
    except grpc.aio.AioRpcError as exc:
        match exc.code():
            case grpc.StatusCode.DEADLINE_EXCEEDED:
                message = (
                    f"The hub at {address} did not answer within {timeout:g} seconds; check HUB_ADDRESS and HUB_TLS"
                )
            case grpc.StatusCode.UNAUTHENTICATED:
                message = f"The hub at {address} rejected HUB_TOKEN"
            case code:
                message = f"The hub at {address} failed with {code.name}: {exc.details()}"
        raise HubError(message) from exc
    return response.version


def _check_version(hub_version: str, client_version: str) -> None:
    if hub_version == "dev":
        _LOGGER.warning("The hub reports no version, so kit cannot check that it offers what this bot calls")
        return
    hub = _major_minor(hub_version)
    if hub is None:
        raise HubError(f"The hub reported an unknown version: {hub_version!r}")
    client = _major_minor(client_version)
    if client is not None and hub < client:
        raise HubError(
            f"The hub runs {hub_version}, but this bot needs {client[0]}.{client[1]} or newer; update the hub first"
        )


def _major_minor(text: str) -> tuple[int, int] | None:
    match = _VERSION.match(text)
    if match is None:
        return None
    return int(match[1]), int(match[2])
