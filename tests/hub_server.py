from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import grpc.aio
from librebots.hub.v1.guilds_pb2_grpc import GuildServiceStub
from librebots.hub.v1.privacy_pb2_grpc import PrivacyServiceStub
from librebots.hub.v1.stats_pb2_grpc import StatsServiceStub

from librebots.kit import Hub


@asynccontextmanager
async def serving(register: Callable[[grpc.aio.Server], None]) -> AsyncGenerator[Hub]:
    server = grpc.aio.server()
    register(server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield Hub(
            channel=channel,
            version="test",
            guilds=GuildServiceStub(channel),
            privacy=PrivacyServiceStub(channel),
            stats=StatsServiceStub(channel),
        )
    finally:
        await channel.close()
        await server.stop(0)
