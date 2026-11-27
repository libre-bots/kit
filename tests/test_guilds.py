from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from unittest.mock import create_autospec

import grpc
import grpc.aio
import hikari
import pytest
from librebots.hub.v1 import guilds_pb2, guilds_pb2_grpc
from librebots.hub.v1.guilds_pb2_grpc import GuildServiceStub
from librebots.hub.v1.privacy_pb2_grpc import PrivacyServiceStub
from librebots.hub.v1.stats_pb2_grpc import StatsServiceStub

from librebots.kit import GuildReporter, Hub, HubError, SyncResult

type Context[Request, Response] = grpc.aio.ServicerContext[Request, Awaitable[Response]]


def on_shard(shard: int, nth: int = 1, count: int = 4) -> int:
    return (count * nth + shard) << 22


class FakeGuilds(guilds_pb2_grpc.GuildServiceServicer):
    def __init__(self) -> None:
        self.log: list[str] = []
        self.joined: list[guilds_pb2.GuildJoinedRequest] = []
        self.synced: list[guilds_pb2.SyncGuildsRequest] = []
        self.attempts: list[str] = []
        self.failing: set[str] = set()
        self.stalled: set[str] = set()
        self.stall_started = asyncio.Event()
        self._never = asyncio.Event()

    async def _check[Request, Response](self, name: str, context: Context[Request, Response]) -> None:
        self.attempts.append(name)
        if name in self.failing:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "hub is down")
        if name in self.stalled:
            self.stall_started.set()
            await self._never.wait()

    async def GuildJoined(
        self,
        request: guilds_pb2.GuildJoinedRequest,
        context: Context[guilds_pb2.GuildJoinedRequest, guilds_pb2.GuildJoinedResponse],
    ) -> guilds_pb2.GuildJoinedResponse:
        await self._check("joined", context)
        self.log.append(f"joined {request.guild_id}")
        self.joined.append(request)
        return guilds_pb2.GuildJoinedResponse(settings=guilds_pb2.GuildSettings(guild_id=request.guild_id, locale="de"))

    async def GuildLeft(
        self,
        request: guilds_pb2.GuildLeftRequest,
        context: Context[guilds_pb2.GuildLeftRequest, guilds_pb2.GuildLeftResponse],
    ) -> guilds_pb2.GuildLeftResponse:
        await self._check("left", context)
        self.log.append(f"left {request.guild_id}")
        return guilds_pb2.GuildLeftResponse()

    async def SyncGuilds(
        self,
        request: guilds_pb2.SyncGuildsRequest,
        context: Context[guilds_pb2.SyncGuildsRequest, guilds_pb2.SyncGuildsResponse],
    ) -> guilds_pb2.SyncGuildsResponse:
        await self._check("sync", context)
        self.log.append(f"sync {','.join(str(guild.guild_id) for guild in request.guilds)}")
        self.synced.append(request)
        return guilds_pb2.SyncGuildsResponse(joined=1, left=2)


def own_guild(guild_id: int, members: int = 1) -> hikari.OwnGuild:
    guild = create_autospec(hikari.OwnGuild, instance=True)
    guild.id = hikari.Snowflake(guild_id)
    guild.approximate_member_count = members
    return guild


class Listing:
    def __init__(self, guilds: list[hikari.OwnGuild]) -> None:
        self.guilds = guilds
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        rest = create_autospec(hikari.api.RESTClient, instance=True)
        rest.fetch_my_guilds.side_effect = self._guilds
        self.rest: hikari.api.RESTClient = rest

    async def _guilds(self) -> AsyncIterator[hikari.OwnGuild]:
        self.started.set()
        await self.proceed.wait()
        for guild in self.guilds:
            yield guild


@pytest.fixture
async def hub() -> AsyncGenerator[tuple[Hub, FakeGuilds]]:
    # The fake answers only the calls guild reporting makes.
    fake = FakeGuilds()  # pyright: ignore[reportAbstractUsage]
    server = grpc.aio.server()
    guilds_pb2_grpc.add_GuildServiceServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield (
            Hub(
                channel=channel,
                version="test",
                guilds=GuildServiceStub(channel),
                privacy=PrivacyServiceStub(channel),
                stats=StatsServiceStub(channel),
            ),
            fake,
        )
    finally:
        await channel.close()
        await server.stop(0)


async def test_reports_a_join_and_returns_the_language(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]

    locale = await reporter.joined(on_shard(0), preferred_locale="fr", member_count=12)

    assert locale == "de"
    request = fake.joined[0]
    assert (request.guild_id, request.preferred_locale, request.member_count) == (on_shard(0), "fr", 12)


async def test_reports_zero_members_when_the_count_is_unknown(hub: tuple[Hub, FakeGuilds]) -> None:
    await GuildReporter(hub[0]).joined(on_shard(0), preferred_locale="en-US", member_count=None)

    assert hub[1].joined[0].member_count == 0


async def test_reports_a_leave(hub: tuple[Hub, FakeGuilds]) -> None:
    await GuildReporter(hub[0]).left(on_shard(2))

    assert hub[1].log == [f"left {on_shard(2)}"]


@pytest.mark.parametrize("call", ["joined", "left", "sync"])
async def test_raises_hub_error_when_the_hub_fails(hub: tuple[Hub, FakeGuilds], call: str) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    fake.failing.add(call)

    with pytest.raises(HubError, match="UNAVAILABLE"):
        if call == "joined":
            await reporter.joined(on_shard(0), preferred_locale="en-US", member_count=1)
        elif call == "left":
            await reporter.left(on_shard(0))
        else:
            await reporter.sync(Listing([]).rest, [0], 1)


async def test_syncs_the_guilds_on_the_given_shards(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    listing = Listing([own_guild(on_shard(shard, nth), 10 * shard + nth) for shard in range(4) for nth in (1, 2)])

    result = await reporter.sync(listing.rest, [3, 1], 4)

    request = fake.synced[0]
    assert list(request.shards.shard_ids) == [1, 3]
    assert request.shards.shard_count == 4
    assert {(guild.guild_id, guild.member_count) for guild in request.guilds} == {
        (on_shard(1, 1), 11),
        (on_shard(1, 2), 12),
        (on_shard(3, 1), 31),
        (on_shard(3, 2), 32),
    }
    assert result == SyncResult(joined=1, left=2)


async def test_sends_no_language_with_a_sync(hub: tuple[Hub, FakeGuilds]) -> None:
    await GuildReporter(hub[0]).sync(Listing([own_guild(on_shard(0))]).rest, [0], 1)

    assert hub[1].synced[0].guilds[0].preferred_locale == ""


async def run_with_paused_listing(reporter: GuildReporter, listing: Listing, during: AsyncIterator[None]) -> None:
    listing.proceed.clear()
    sync = asyncio.create_task(reporter.sync(listing.rest, [0], 1))
    async with asyncio.timeout(5):
        await listing.started.wait()
        async for _ in during:
            pass
        listing.proceed.set()
        await sync


async def test_reports_a_join_again_after_a_sync_that_missed_it(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    old, new = on_shard(0, 1), on_shard(0, 2)

    async def join() -> AsyncIterator[None]:
        await reporter.joined(new, preferred_locale="en-US", member_count=3)
        yield

    await run_with_paused_listing(reporter, Listing([own_guild(old)]), join())

    assert fake.log == [f"joined {new}", f"sync {old}", f"joined {new}"]


async def test_reports_a_leave_again_after_a_sync_that_still_listed_the_guild(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    guild = on_shard(0)

    async def leave() -> AsyncIterator[None]:
        await reporter.left(guild)
        yield

    await run_with_paused_listing(reporter, Listing([own_guild(guild)]), leave())

    assert fake.log == [f"left {guild}", f"sync {guild}", f"left {guild}"]


async def test_reports_only_the_latest_change_of_a_guild_again(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    guild = on_shard(0)

    async def join_and_leave() -> AsyncIterator[None]:
        await reporter.joined(guild, preferred_locale="en-US", member_count=3)
        await reporter.left(guild)
        yield

    await run_with_paused_listing(reporter, Listing([]), join_and_leave())

    assert fake.log == [f"joined {guild}", f"left {guild}", "sync ", f"left {guild}"]


async def test_reports_changes_before_a_sync_only_once(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    guild = on_shard(0)

    await reporter.joined(guild, preferred_locale="en-US", member_count=3)
    await reporter.sync(Listing([own_guild(guild)]).rest, [0], 1)

    assert fake.log == [f"joined {guild}", f"sync {guild}"]


async def test_keeps_a_repeated_report_that_fails_for_the_next_sync(
    hub: tuple[Hub, FakeGuilds], caplog: pytest.LogCaptureFixture
) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    joined, left = on_shard(0, 1), on_shard(0, 2)

    async def change_then_break_joins() -> AsyncIterator[None]:
        await reporter.joined(joined, preferred_locale="en-US", member_count=3)
        await reporter.left(left)
        fake.failing.add("joined")
        yield

    with caplog.at_level(logging.WARNING, logger="librebots.kit"):
        await run_with_paused_listing(reporter, Listing([]), change_then_break_joins())

    assert fake.log == [f"joined {joined}", f"left {left}", "sync ", f"left {left}"]
    assert fake.attempts.count("joined") == 2
    assert str(joined) in caplog.text

    fake.failing.clear()
    assert await reporter.sync(Listing([]).rest, [0], 1) == SyncResult(joined=1, left=2)

    assert fake.log[-2:] == ["sync ", f"joined {joined}"]


async def test_runs_one_sync_at_a_time(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    first, second = Listing([own_guild(on_shard(0, 1))]), Listing([own_guild(on_shard(0, 2))])
    first.proceed.clear()
    attempted = asyncio.Event()

    async def second_sync() -> SyncResult:
        attempted.set()
        return await reporter.sync(second.rest, [0], 1)

    async with asyncio.timeout(5):
        running = asyncio.create_task(reporter.sync(first.rest, [0], 1))
        await first.started.wait()
        waiting = asyncio.create_task(second_sync())
        await attempted.wait()
        started_early = second.started.is_set()
        first.proceed.set()
        await asyncio.gather(running, waiting)

    assert not started_early
    assert fake.log == [f"sync {on_shard(0, 1)}", f"sync {on_shard(0, 2)}"]


async def test_gives_up_on_a_hub_that_does_not_answer(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0], timeout=0.2), hub[1]
    fake.stalled.add("sync")

    with pytest.raises(HubError, match="DEADLINE_EXCEEDED"):
        await reporter.sync(Listing([]).rest, [0], 1)

    fake.stalled.clear()
    assert await reporter.sync(Listing([]).rest, [0], 1) == SyncResult(joined=1, left=2)


async def test_keeps_the_changes_of_a_failed_sync_for_the_next_one(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    guild = on_shard(0)

    async def join_then_break_the_sync() -> AsyncIterator[None]:
        await reporter.joined(guild, preferred_locale="en-US", member_count=3)
        fake.failing.add("sync")
        yield

    with pytest.raises(HubError):
        await run_with_paused_listing(reporter, Listing([]), join_then_break_the_sync())
    fake.failing.clear()
    await reporter.sync(Listing([]).rest, [0], 1)

    assert fake.log == [f"joined {guild}", "sync ", f"joined {guild}"]


async def test_keeps_only_the_latest_change_after_a_failed_sync(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    guild = on_shard(0)

    async def join_then_break_the_sync() -> AsyncIterator[None]:
        await reporter.joined(guild, preferred_locale="en-US", member_count=3)
        fake.failing.add("sync")
        yield

    with pytest.raises(HubError):
        await run_with_paused_listing(reporter, Listing([]), join_then_break_the_sync())
    fake.failing.clear()
    await reporter.left(guild)
    await reporter.sync(Listing([]).rest, [0], 1)

    assert fake.log == [f"joined {guild}", f"left {guild}", "sync ", f"left {guild}"]


async def test_keeps_the_changes_a_cancelled_sync_did_not_send_again(hub: tuple[Hub, FakeGuilds]) -> None:
    reporter, fake = GuildReporter(hub[0]), hub[1]
    first, second = on_shard(0, 1), on_shard(0, 2)
    listing = Listing([own_guild(first), own_guild(second)])
    listing.proceed.clear()

    async with asyncio.timeout(5):
        sync = asyncio.create_task(reporter.sync(listing.rest, [0], 1))
        await listing.started.wait()
        await reporter.left(first)
        await reporter.left(second)
        fake.stalled.add("left")
        listing.proceed.set()
        await fake.stall_started.wait()
        sync.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sync
        fake.stalled.clear()
        await reporter.sync(Listing([]).rest, [0], 1)

    assert fake.log[-3:] == ["sync ", f"left {first}", f"left {second}"]
