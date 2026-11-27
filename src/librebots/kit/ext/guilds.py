"""Reports joins and leaves to the hub, and syncs a shard's guilds when it opens a session and daily at 04:00 UTC.

Needs a `GuildReporter` in lightbulb's dependency injection.
"""

from __future__ import annotations

import logging
from collections.abc import Collection

import hikari
import lightbulb

from librebots.kit.guilds import GuildReporter
from librebots.kit.hub import HubError

__all__ = ["loader", "sync_own_shards"]

_LOGGER = logging.getLogger(__name__)

loader = lightbulb.Loader()


@loader.listener(hikari.GuildJoinEvent)
async def _report_join(event: hikari.GuildJoinEvent, reporter: GuildReporter = lightbulb.di.INJECTED) -> None:
    guild = event.guild
    try:
        await reporter.joined(guild.id, preferred_locale=str(guild.preferred_locale), member_count=guild.member_count)
    except HubError as exc:
        _LOGGER.warning("Could not report joining guild %s, the next sync will: %s", guild.id, exc)


@loader.listener(hikari.GuildLeaveEvent)
async def _report_leave(event: hikari.GuildLeaveEvent, reporter: GuildReporter = lightbulb.di.INJECTED) -> None:
    try:
        await reporter.left(event.guild_id)
    except HubError as exc:
        _LOGGER.warning("Could not report leaving guild %s, the next sync will: %s", event.guild_id, exc)


@loader.listener(hikari.ShardReadyEvent)
async def _sync_shard(
    event: hikari.ShardReadyEvent,
    reporter: GuildReporter = lightbulb.di.INJECTED,
    rest: hikari.api.RESTClient = lightbulb.di.INJECTED,
) -> None:
    await _sync(reporter, rest, [event.shard.id], event.shard.shard_count)


@lightbulb.di.with_di
async def sync_own_shards(
    bot: hikari.GatewayBot = lightbulb.di.INJECTED,
    reporter: GuildReporter = lightbulb.di.INJECTED,
    rest: hikari.api.RESTClient = lightbulb.di.INJECTED,
) -> None:
    """Sync the guilds of every shard this process runs, as the extension does daily at 04:00 UTC.

    Takes its arguments from lightbulb's dependency injection, so call it from a command, listener or task.
    """
    await _sync(reporter, rest, bot.shards.keys(), bot.shard_count)


# With lightbulb's default of one failure, a single error would stop the daily sync until the next restart.
loader.task(lightbulb.crontrigger("0 4 * * *"), max_failures=-1)(sync_own_shards)


async def _sync(
    reporter: GuildReporter, rest: hikari.api.RESTClient, shard_ids: Collection[int], shard_count: int
) -> None:
    shards = sorted(shard_ids)
    try:
        result = await reporter.sync(rest, shards, shard_count)
    except HubError as exc:
        _LOGGER.warning("Could not sync the guilds of shards %s: %s", shards, exc)
        return
    _LOGGER.info("Synced the guilds of shards %s: %d joined, %d left", shards, result.joined, result.left)
