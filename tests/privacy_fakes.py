from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import grpc
import grpc.aio
from librebots.hub.v1 import privacy_pb2, privacy_pb2_grpc

type Context[Request, Response] = grpc.aio.ServicerContext[Request, Awaitable[Response]]


def counts(**values: int) -> list[privacy_pb2.CategoryCount]:
    return [privacy_pb2.CategoryCount(category=name.replace("_", "-"), count=count) for name, count in values.items()]


class FakePrivacy(privacy_pb2_grpc.PrivacyServiceServicer):
    def __init__(self) -> None:
        self.log: list[str] = []
        self.pending: list[int] = []
        self.user_data = counts(starboard_entries=12, starboard_stars=87)
        self.guild_data = counts(starboard_settings=1, starboard_entries=342)
        self.failing: set[str] = set()
        self.stalled: set[str] = set()
        self._never = asyncio.Event()

    async def _check[Request, Response](self, name: str, context: Context[Request, Response]) -> None:
        if name in self.failing:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "hub is down")
        if name in self.stalled:
            await self._never.wait()

    async def CountUserData(
        self,
        request: privacy_pb2.CountUserDataRequest,
        context: Context[privacy_pb2.CountUserDataRequest, privacy_pb2.CountUserDataResponse],
    ) -> privacy_pb2.CountUserDataResponse:
        await self._check("count user", context)
        self.log.append(f"count user {request.user_id}")
        return privacy_pb2.CountUserDataResponse(categories=self.user_data)

    async def ExportUserData(
        self,
        request: privacy_pb2.ExportUserDataRequest,
        context: Context[privacy_pb2.ExportUserDataRequest, privacy_pb2.ExportUserDataResponse],
    ) -> privacy_pb2.ExportUserDataResponse:
        await self._check("export user", context)
        return privacy_pb2.ExportUserDataResponse(
            json=f'{{"bot":"starboard","user_id":"{request.user_id}","data":{{}}}}'
        )

    async def DeleteUserData(
        self,
        request: privacy_pb2.DeleteUserDataRequest,
        context: Context[privacy_pb2.DeleteUserDataRequest, privacy_pb2.DeleteUserDataResponse],
    ) -> privacy_pb2.DeleteUserDataResponse:
        await self._check("delete user", context)
        self.log.append(f"delete user {request.user_id}")
        if request.user_id in self.pending:
            self.pending.remove(request.user_id)
        return privacy_pb2.DeleteUserDataResponse(deleted=counts(starboard_entries=12))

    async def RequestUserDeletion(
        self,
        request: privacy_pb2.RequestUserDeletionRequest,
        context: Context[privacy_pb2.RequestUserDeletionRequest, privacy_pb2.RequestUserDeletionResponse],
    ) -> privacy_pb2.RequestUserDeletionResponse:
        await self._check("request deletion", context)
        self.log.append(f"request deletion {request.user_id}")
        if request.user_id not in self.pending:
            self.pending.append(request.user_id)
        return privacy_pb2.RequestUserDeletionResponse()

    async def ListPendingUserDeletions(
        self,
        request: privacy_pb2.ListPendingUserDeletionsRequest,
        context: Context[privacy_pb2.ListPendingUserDeletionsRequest, privacy_pb2.ListPendingUserDeletionsResponse],
    ) -> privacy_pb2.ListPendingUserDeletionsResponse:
        await self._check("list pending", context)
        deletions = [privacy_pb2.PendingUserDeletion(user_id=user_id) for user_id in self.pending]
        return privacy_pb2.ListPendingUserDeletionsResponse(deletions=deletions)

    async def CountGuildData(
        self,
        request: privacy_pb2.CountGuildDataRequest,
        context: Context[privacy_pb2.CountGuildDataRequest, privacy_pb2.CountGuildDataResponse],
    ) -> privacy_pb2.CountGuildDataResponse:
        await self._check("count guild", context)
        self.log.append(f"count guild {request.guild_id}")
        return privacy_pb2.CountGuildDataResponse(categories=self.guild_data)

    async def DeleteGuildData(
        self,
        request: privacy_pb2.DeleteGuildDataRequest,
        context: Context[privacy_pb2.DeleteGuildDataRequest, privacy_pb2.DeleteGuildDataResponse],
    ) -> privacy_pb2.DeleteGuildDataResponse:
        await self._check("delete guild", context)
        self.log.append(f"delete guild {request.guild_id}")
        return privacy_pb2.DeleteGuildDataResponse(deleted=self.guild_data)
