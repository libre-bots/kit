from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from unittest.mock import create_autospec

import hikari
import lightbulb
import pytest
from guild_fakes import FakeGuilds, Listing, on_shard, own_guild
from hub_server import serving
from librebots.hub.v1 import guilds_pb2_grpc
from librebots.hub.v1.shards_pb2 import ShardSet

from librebots.kit import GuildReporter
from librebots.kit.ext import guilds

EXTENSION = "librebots.kit.ext.guilds"

type Listener[E: hikari.Event] = Callable[[E], Awaitable[None]]


@pytest.fixture
async def running() -> AsyncGenerator[tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds]]:
    # The fake answers only the calls guild reporting makes.
    fake = FakeGuilds()  # pyright: ignore[reportAbstractUsage]
    async with serving(lambda server: guilds_pb2_grpc.add_GuildServiceServicer_to_server(fake, server)) as hub:
        bot = hikari.GatewayBot("MTIzNDU2Nzg5MDEyMzQ1Njc4.x.y", banner=None)
        client = lightbulb.client_from_app(bot)
        client.di.registry_for(lightbulb.di.Contexts.DEFAULT).register_value(GuildReporter, GuildReporter(hub))
        await client.load_extensions(EXTENSION)
        yield bot, client, fake
        await client.unload_extensions(EXTENSION)


def listener[E: hikari.Event](bot: hikari.GatewayBot, event_type: type[E]) -> Listener[E]:
    (callback,) = bot.event_manager.get_listeners(event_type)
    return callback


def provide[T](client: lightbulb.Client, typ: type[T], value: T) -> None:
    client.di.registry_for(lightbulb.di.Contexts.DEFAULT).register_value(typ, value)


def join_event(guild_id: int, locale: hikari.Locale, members: int | None) -> hikari.GuildJoinEvent:
    guild = create_autospec(hikari.GatewayGuild, instance=True)
    guild.id = hikari.Snowflake(guild_id)
    guild.preferred_locale = locale
    guild.member_count = members
    event = create_autospec(hikari.GuildJoinEvent, instance=True)
    event.guild = guild
    return event


def leave_event(guild_id: int) -> hikari.GuildLeaveEvent:
    event = create_autospec(hikari.GuildLeaveEvent, instance=True)
    event.guild_id = hikari.Snowflake(guild_id)
    return event


def ready_event(shard_id: int, shard_count: int) -> hikari.ShardReadyEvent:
    shard = create_autospec(hikari.api.GatewayShard, instance=True)
    shard.id = shard_id
    shard.shard_count = shard_count
    event = create_autospec(hikari.ShardReadyEvent, instance=True)
    event.shard = shard
    return event


def sharded_bot(shard_ids: list[int], shard_count: int) -> hikari.GatewayBot:
    bot = create_autospec(hikari.GatewayBot, instance=True)
    bot.shards = {shard_id: create_autospec(hikari.api.GatewayShard, instance=True) for shard_id in shard_ids}
    bot.shard_count = shard_count
    return bot


def guilds_on_every_shard() -> Listing:
    return Listing([own_guild(on_shard(shard), members=shard + 1) for shard in range(4)])


@pytest.mark.parametrize("event_type", [hikari.GuildJoinEvent, hikari.GuildLeaveEvent, hikari.ShardReadyEvent])
async def test_listens_once_loaded(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds], event_type: type[hikari.Event]
) -> None:
    assert listener(running[0], event_type)


async def test_reports_a_join_with_the_language_and_member_count(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds],
) -> None:
    bot, _, fake = running

    await listener(bot, hikari.GuildJoinEvent)(join_event(on_shard(1), hikari.Locale.DE, 42))

    request = fake.joined[0]
    assert (request.guild_id, request.preferred_locale, request.member_count) == (on_shard(1), "de", 42)


async def test_reports_a_leave(running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds]) -> None:
    bot, _, fake = running

    await listener(bot, hikari.GuildLeaveEvent)(leave_event(on_shard(3)))

    assert fake.log == [f"left {on_shard(3)}"]


@pytest.mark.parametrize(
    ("call", "event_type", "event"),
    [
        ("joined", hikari.GuildJoinEvent, join_event(on_shard(0), hikari.Locale.EN_US, 1)),
        ("left", hikari.GuildLeaveEvent, leave_event(on_shard(0))),
    ],
)
async def test_logs_a_report_the_hub_fails(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds],
    caplog: pytest.LogCaptureFixture,
    call: str,
    event_type: type[hikari.Event],
    event: hikari.Event,
) -> None:
    bot, _, fake = running
    fake.failing.add(call)

    with caplog.at_level(logging.WARNING, logger=guilds.__name__):
        await listener(bot, event_type)(event)

    assert str(on_shard(0)) in caplog.text
    assert "UNAVAILABLE" in caplog.text


async def test_syncs_the_shard_of_a_new_session(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds], caplog: pytest.LogCaptureFixture
) -> None:
    bot, client, fake = running
    provide(client, hikari.api.RESTClient, guilds_on_every_shard().rest)

    with caplog.at_level(logging.INFO, logger=guilds.__name__):
        await listener(bot, hikari.ShardReadyEvent)(ready_event(2, 4))

    request = fake.synced[0]
    assert request.shards == ShardSet(shard_ids=[2], shard_count=4)
    assert [guild.guild_id for guild in request.guilds] == [on_shard(2)]
    assert "1 joined, 2 left" in caplog.text


async def test_logs_a_sync_the_hub_fails(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds], caplog: pytest.LogCaptureFixture
) -> None:
    bot, client, fake = running
    provide(client, hikari.api.RESTClient, guilds_on_every_shard().rest)
    fake.failing.add("sync")

    with caplog.at_level(logging.WARNING, logger=guilds.__name__):
        await listener(bot, hikari.ShardReadyEvent)(ready_event(2, 4))

    assert "shards [2]" in caplog.text
    assert "UNAVAILABLE" in caplog.text


async def test_syncs_every_own_shard_once_a_day(
    running: tuple[hikari.GatewayBot, lightbulb.Client, FakeGuilds],
) -> None:
    _, client, fake = running
    provide(client, hikari.GatewayBot, sharded_bot([0, 2], 4))
    provide(client, hikari.api.RESTClient, guilds_on_every_shard().rest)

    async with client.di.enter_context(lightbulb.di.Contexts.DEFAULT):
        await guilds.sync_own_shards()

    request = fake.synced[0]
    assert request.shards == ShardSet(shard_ids=[0, 2], shard_count=4)
    assert [guild.guild_id for guild in request.guilds] == [on_shard(0), on_shard(2)]
