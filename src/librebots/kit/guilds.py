from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterable, Awaitable, Callable, Collection
from typing import Protocol

import grpc.aio
import msgspec
from librebots.hub.v1.guilds_pb2 import GuildJoinedRequest, GuildLeftRequest, SyncedGuild, SyncGuildsRequest
from librebots.hub.v1.shards_pb2 import ShardSet

from librebots.kit.hub import Hub, HubError

__all__ = ["GuildLister", "GuildReporter", "ListedGuild", "SyncResult"]

_LOGGER = logging.getLogger(__name__)


class ListedGuild(Protocol):
    """A guild as Discord's guild list returns it, such as hikari's `OwnGuild`."""

    @property
    def id(self) -> int: ...

    @property
    def approximate_member_count(self) -> int: ...


class GuildLister(Protocol):
    """Anything that lists the bot's guilds with their member counts, such as hikari's `RESTClient`."""

    def fetch_my_guilds(self) -> AsyncIterable[ListedGuild]: ...


class SyncResult(msgspec.Struct, frozen=True, kw_only=True):
    """How many guilds a sync registered and how many it marked as left."""

    joined: int
    left: int


class GuildReporter:
    """Reports to the hub which guilds the shards of this process are in.

    Call `joined` and `left` for the gateway's join and leave events, and `sync` once the shards have started, after
    a shard opens a new session and once a day. A join or leave reported while a sync runs is sent again after the
    next sync that succeeds, so the older guild list a sync read cannot undo it. Every call to the hub gives up after
    `timeout` seconds.
    """

    def __init__(self, hub: Hub, *, timeout: float = 30) -> None:
        self._hub = hub
        self._timeout = timeout
        self._sync_lock = asyncio.Lock()
        self._pending: dict[int, Callable[[], Awaitable[object]]] = {}

    async def joined(self, guild_id: int, *, preferred_locale: str, member_count: int | None) -> str:
        """Report that the bot joined a guild and return the guild's language. Raises `HubError` if the hub fails."""
        report = functools.partial(self._report_join, guild_id, preferred_locale, member_count)
        self._remember(guild_id, report)
        return await report()

    async def left(self, guild_id: int) -> None:
        """Report that the bot left or was removed from a guild. Raises `HubError` if the hub fails."""
        report = functools.partial(self._report_leave, guild_id)
        self._remember(guild_id, report)
        await report()

    async def sync(self, rest: GuildLister, shard_ids: Collection[int], shard_count: int) -> SyncResult:
        """Send every guild on these shards, and the hub marks the bot's other guilds on them as left.

        Syncs run one at a time. Raises `HubError` if the hub fails.
        """
        shards = frozenset(shard_ids)
        async with self._sync_lock:
            guilds = [
                SyncedGuild(guild_id=guild.id, member_count=guild.approximate_member_count)
                async for guild in rest.fetch_my_guilds()
                if (guild.id >> 22) % shard_count in shards
            ]
            request = SyncGuildsRequest(
                shards=ShardSet(shard_ids=sorted(shards), shard_count=shard_count), guilds=guilds
            )
            response = await _call("SyncGuilds", self._hub.guilds.SyncGuilds(request, timeout=self._timeout))
            await self._report_again()
        return SyncResult(joined=response.joined, left=response.left)

    def _remember(self, guild_id: int, report: Callable[[], Awaitable[object]]) -> None:
        if self._sync_lock.locked() or guild_id in self._pending:
            self._pending[guild_id] = report

    async def _report_again(self) -> None:
        while self._pending:
            guild_id, report = next(iter(self._pending.items()))
            try:
                await report()
            except HubError as exc:
                _LOGGER.warning("Could not report guild %s again after a sync: %s", guild_id, exc)
            if self._pending.get(guild_id) is report:
                del self._pending[guild_id]

    async def _report_join(self, guild_id: int, preferred_locale: str, member_count: int | None) -> str:
        request = GuildJoinedRequest(
            guild_id=guild_id, preferred_locale=preferred_locale, member_count=member_count or 0
        )
        response = await _call("GuildJoined", self._hub.guilds.GuildJoined(request, timeout=self._timeout))
        return response.settings.locale

    async def _report_leave(self, guild_id: int) -> None:
        request = GuildLeftRequest(guild_id=guild_id)
        await _call("GuildLeft", self._hub.guilds.GuildLeft(request, timeout=self._timeout))


async def _call[T](method: str, call: Awaitable[T]) -> T:
    try:
        return await call
    except grpc.aio.AioRpcError as exc:
        raise HubError(f"{method} failed with {exc.code().name}: {exc.details()}") from exc
