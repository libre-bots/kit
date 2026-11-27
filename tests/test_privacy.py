from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

import pytest
from hub_server import serving
from librebots.hub.v1 import privacy_pb2_grpc
from privacy_fakes import FakePrivacy

from librebots.kit import Hub, HubError, Privacy

USER = 123456789012345678
OTHER = 223456789012345678
GUILD = 987654321098765432


class Removal:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.failures = 0
        self.cancelled = False

    async def __call__(self, user_id: int) -> None:
        self.started.set()
        try:
            await self.proceed.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.failures:
            self.failures -= 1
            raise RuntimeError("Discord refused to delete a post")
        self.log.append(f"remove from discord {user_id}")


@pytest.fixture
async def hub() -> AsyncGenerator[tuple[Hub, FakePrivacy]]:
    fake = FakePrivacy()
    async with serving(lambda server: privacy_pb2_grpc.add_PrivacyServiceServicer_to_server(fake, server)) as hub:
        yield hub, fake


async def test_counts_what_is_stored_about_a_user(hub: tuple[Hub, FakePrivacy]) -> None:
    privacy, fake = Privacy(hub[0]), hub[1]

    assert await privacy.count_user(USER) == {"starboard-entries": 12, "starboard-stars": 87}
    assert fake.log == [f"count user {USER}"]


async def test_exports_the_json_of_the_hub_unchanged(hub: tuple[Hub, FakePrivacy]) -> None:
    exported = await Privacy(hub[0]).export_user(USER)

    assert exported == f'{{"bot":"starboard","user_id":"{USER}","data":{{}}}}'


async def test_counts_and_deletes_what_is_stored_about_a_guild(hub: tuple[Hub, FakePrivacy]) -> None:
    privacy, fake = Privacy(hub[0]), hub[1]

    assert await privacy.count_guild(GUILD) == {"starboard-settings": 1, "starboard-entries": 342}
    assert await privacy.delete_guild(GUILD) == {"starboard-settings": 1, "starboard-entries": 342}
    assert fake.log == [f"count guild {GUILD}", f"delete guild {GUILD}"]


@pytest.mark.parametrize("call", ["count user", "export user", "count guild", "delete guild", "request deletion"])
async def test_raises_hub_error_when_the_hub_fails(hub: tuple[Hub, FakePrivacy], call: str) -> None:
    privacy, fake = Privacy(hub[0]), hub[1]
    fake.failing.add(call)

    with pytest.raises(HubError, match="UNAVAILABLE"):
        match call:
            case "count user":
                await privacy.count_user(USER)
            case "export user":
                await privacy.export_user(USER)
            case "count guild":
                await privacy.count_guild(GUILD)
            case "delete guild":
                await privacy.delete_guild(GUILD)
            case _:
                await privacy.delete_user(USER)


async def test_gives_up_on_a_hub_that_does_not_answer(hub: tuple[Hub, FakePrivacy]) -> None:
    privacy, fake = Privacy(hub[0], timeout=0.2), hub[1]
    fake.stalled.add("count user")

    with pytest.raises(HubError, match="DEADLINE_EXCEEDED"):
        await privacy.count_user(USER)


async def test_deletes_a_user_after_removing_what_the_bot_keeps_on_discord(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    privacy = Privacy(hub[0], on_user_deletion=Removal(fake.log))

    await privacy.delete_user(USER)
    async with asyncio.timeout(5):
        await privacy.wait_for_deletions()

    assert fake.log == [f"request deletion {USER}", f"remove from discord {USER}", f"delete user {USER}"]
    assert fake.pending == []


async def test_deletes_a_user_directly_without_a_removal_step(hub: tuple[Hub, FakePrivacy]) -> None:
    privacy, fake = Privacy(hub[0]), hub[1]

    await privacy.delete_user(USER)
    async with asyncio.timeout(5):
        await privacy.wait_for_deletions()

    assert fake.log == [f"request deletion {USER}", f"delete user {USER}"]


async def test_returns_once_the_deletion_is_recorded(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.proceed.clear()
    privacy = Privacy(hub[0], on_user_deletion=removal)

    await privacy.delete_user(USER)

    assert fake.pending == [USER]
    async with asyncio.timeout(5):
        await removal.started.wait()
        removal.proceed.set()
        await privacy.wait_for_deletions()
    assert fake.pending == []


async def test_starts_no_work_when_the_request_cannot_be_recorded(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    privacy = Privacy(hub[0], on_user_deletion=removal)
    fake.failing.add("request deletion")

    with pytest.raises(HubError):
        await privacy.delete_user(USER)
    await privacy.wait_for_deletions()

    assert not removal.started.is_set()
    assert fake.log == []


async def test_runs_one_deletion_per_user_at_a_time(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.proceed.clear()
    privacy = Privacy(hub[0], on_user_deletion=removal)

    async with asyncio.timeout(5):
        await privacy.delete_user(USER)
        await removal.started.wait()
        await privacy.delete_user(USER)
        removal.proceed.set()
        await privacy.wait_for_deletions()

    assert fake.log.count(f"remove from discord {USER}") == 1
    assert fake.log.count(f"delete user {USER}") == 1


async def test_keeps_a_failed_deletion_pending_and_logs_it(
    hub: tuple[Hub, FakePrivacy], caplog: pytest.LogCaptureFixture
) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.failures = 1
    privacy = Privacy(hub[0], on_user_deletion=removal)

    with caplog.at_level(logging.WARNING, logger="librebots.kit"):
        await privacy.delete_user(USER)
        async with asyncio.timeout(5):
            await privacy.wait_for_deletions()

    assert fake.pending == [USER]
    assert f"delete user {USER}" not in fake.log
    assert str(USER) in caplog.text


async def test_keeps_the_deletion_pending_when_the_hub_fails_to_delete(hub: tuple[Hub, FakePrivacy]) -> None:
    privacy, fake = Privacy(hub[0]), hub[1]
    fake.failing.add("delete user")

    await privacy.delete_user(USER)
    async with asyncio.timeout(5):
        await privacy.wait_for_deletions()

    assert fake.pending == [USER]


async def test_resumes_the_pending_deletions(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    fake.pending = [USER, OTHER]
    privacy = Privacy(hub[0], on_user_deletion=Removal(fake.log))

    started = await privacy.resume_deletions()
    async with asyncio.timeout(5):
        await privacy.wait_for_deletions()

    assert started == 2
    assert fake.pending == []
    assert {f"delete user {USER}", f"delete user {OTHER}"} <= set(fake.log)


async def test_resumes_a_deletion_that_failed_before(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.failures = 1
    privacy = Privacy(hub[0], on_user_deletion=removal)

    await privacy.delete_user(USER)
    async with asyncio.timeout(5):
        await privacy.wait_for_deletions()
        await privacy.resume_deletions()
        await privacy.wait_for_deletions()

    assert fake.pending == []
    assert fake.log[-1] == f"delete user {USER}"


async def test_does_not_resume_a_deletion_that_is_running(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.proceed.clear()
    privacy = Privacy(hub[0], on_user_deletion=removal)

    async with asyncio.timeout(5):
        await privacy.delete_user(USER)
        await removal.started.wait()
        started = await privacy.resume_deletions()
        removal.proceed.set()
        await privacy.wait_for_deletions()

    assert started == 0
    assert fake.log.count(f"delete user {USER}") == 1


async def test_close_stops_running_deletions_and_leaves_them_pending(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    removal = Removal(fake.log)
    removal.proceed.clear()
    privacy = Privacy(hub[0], on_user_deletion=removal)

    async with asyncio.timeout(5):
        await privacy.delete_user(USER)
        await removal.started.wait()
        await privacy.close()

    assert removal.cancelled
    assert fake.pending == [USER]
    assert f"delete user {USER}" not in fake.log


async def test_runs_the_removal_again_when_the_hub_failed_after_it(hub: tuple[Hub, FakePrivacy]) -> None:
    fake = hub[1]
    privacy = Privacy(hub[0], on_user_deletion=Removal(fake.log))
    fake.failing.add("delete user")

    async with asyncio.timeout(5):
        await privacy.delete_user(USER)
        await privacy.wait_for_deletions()
        fake.failing.clear()
        await privacy.resume_deletions()
        await privacy.wait_for_deletions()

    assert fake.log == [
        f"request deletion {USER}",
        f"remove from discord {USER}",
        f"remove from discord {USER}",
        f"delete user {USER}",
    ]
