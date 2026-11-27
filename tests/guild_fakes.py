from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from unittest.mock import create_autospec

import grpc
import grpc.aio
import hikari
from librebots.hub.v1 import guilds_pb2, guilds_pb2_grpc

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
