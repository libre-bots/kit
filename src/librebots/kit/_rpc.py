from __future__ import annotations

from collections.abc import Awaitable

import grpc.aio

from librebots.kit.hub import HubError


async def call[T](method: str, request: Awaitable[T]) -> T:
    try:
        return await request
    except grpc.aio.AioRpcError as exc:
        raise HubError(f"{method} failed with {exc.code().name}: {exc.details()}") from exc
