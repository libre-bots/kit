from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from librebots.hub.v1.privacy_pb2 import (
    CategoryCount,
    CountGuildDataRequest,
    CountUserDataRequest,
    DeleteGuildDataRequest,
    DeleteUserDataRequest,
    ExportUserDataRequest,
    ListPendingUserDeletionsRequest,
    RequestUserDeletionRequest,
)

from librebots.kit._rpc import call
from librebots.kit.hub import Hub

__all__ = ["Privacy"]

_LOGGER = logging.getLogger(__name__)


class Privacy:
    """Counts, exports and deletes what the bot stores in the hub about a person or a guild.

    Deleting a person's data runs in the background and survives a restart: the hub records the request first, then
    `on_user_deletion` removes what the bot keeps on Discord for that person, then the hub deletes the rest. A deletion
    that fails stays pending, and `resume_deletions` starts it again. `on_user_deletion` can therefore run several
    times for one person, also after an earlier run removed some or all of it, and must succeed when nothing is left
    to remove. Every call to the hub gives up after `timeout` seconds.
    """

    def __init__(
        self,
        hub: Hub,
        *,
        on_user_deletion: Callable[[int], Awaitable[None]] | None = None,
        timeout: float = 30,
    ) -> None:
        self._hub = hub
        self._on_user_deletion = on_user_deletion
        self._timeout = timeout
        self._deletions: dict[int, asyncio.Task[None]] = {}

    async def count_user(self, user_id: int) -> dict[str, int]:
        """Return what the bot stores about a person, counted by category. Raises `HubError` if the hub fails."""
        request = CountUserDataRequest(user_id=user_id)
        response = await call("CountUserData", self._hub.privacy.CountUserData(request, timeout=self._timeout))
        return _by_category(response.categories)

    async def export_user(self, user_id: int) -> str:
        """Return everything the bot stores about a person as a JSON document. Raises `HubError` if the hub fails."""
        request = ExportUserDataRequest(user_id=user_id)
        response = await call("ExportUserData", self._hub.privacy.ExportUserData(request, timeout=self._timeout))
        return response.json

    async def delete_user(self, user_id: int) -> None:
        """Record that a person's data is to be deleted and start deleting it in the background.

        Returns once the hub has recorded the request. Raises `HubError` if it cannot.
        """
        request = RequestUserDeletionRequest(user_id=user_id)
        await call("RequestUserDeletion", self._hub.privacy.RequestUserDeletion(request, timeout=self._timeout))
        self._start(user_id)

    async def resume_deletions(self) -> int:
        """Start every pending deletion that is not running yet and return how many it started.

        Raises `HubError` if the hub fails.
        """
        request = ListPendingUserDeletionsRequest()
        response = await call(
            "ListPendingUserDeletions", self._hub.privacy.ListPendingUserDeletions(request, timeout=self._timeout)
        )
        return sum(self._start(deletion.user_id) for deletion in response.deletions)

    async def wait_for_deletions(self) -> None:
        """Wait until every running deletion has finished or failed."""
        while self._deletions:
            await asyncio.wait(list(self._deletions.values()))

    async def count_guild(self, guild_id: int) -> dict[str, int]:
        """Return what the bot stores about a guild, counted by category. Raises `HubError` if the hub fails."""
        request = CountGuildDataRequest(guild_id=guild_id)
        response = await call("CountGuildData", self._hub.privacy.CountGuildData(request, timeout=self._timeout))
        return _by_category(response.categories)

    async def delete_guild(self, guild_id: int) -> dict[str, int]:
        """Delete what the bot stores about a guild and return what was deleted. Raises `HubError` if the hub fails."""
        request = DeleteGuildDataRequest(guild_id=guild_id)
        response = await call("DeleteGuildData", self._hub.privacy.DeleteGuildData(request, timeout=self._timeout))
        return _by_category(response.deleted)

    async def close(self) -> None:
        """Stop the running deletions; they stay pending in the hub."""
        tasks = list(self._deletions.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start(self, user_id: int) -> bool:
        if user_id in self._deletions:
            return False
        task = asyncio.create_task(self._delete(user_id), name=f"delete the data of user {user_id}")
        self._deletions[user_id] = task
        task.add_done_callback(lambda done: self._forget(user_id, done))
        return True

    def _forget(self, user_id: int, task: asyncio.Task[None]) -> None:
        if self._deletions.get(user_id) is task:
            del self._deletions[user_id]

    async def _delete(self, user_id: int) -> None:
        try:
            if self._on_user_deletion is not None:
                await self._on_user_deletion(user_id)
            request = DeleteUserDataRequest(user_id=user_id)
            await call("DeleteUserData", self._hub.privacy.DeleteUserData(request, timeout=self._timeout))
        except Exception:
            _LOGGER.exception("Deleting the data of user %s failed; it stays pending and is tried again later", user_id)


def _by_category(categories: Iterable[CategoryCount]) -> dict[str, int]:
    return {category.category: category.count for category in categories}
