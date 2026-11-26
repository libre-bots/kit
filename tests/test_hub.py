import logging
import socket
from collections.abc import AsyncGenerator, Mapping
from importlib.metadata import version
from pathlib import Path

import grpc
import grpc.aio
import hikari
import pytest
from librebots.hub.v1 import info_pb2, info_pb2_grpc

from librebots.kit import Config, HubError, connect_hub

TOKEN = "hub-token"
CLIENT = version("librebots-hub")
CLIENT_MAJOR, CLIENT_MINOR = (int(part) for part in CLIENT.split(".")[:2])


class FakeInfo(info_pb2_grpc.InfoServiceServicer):
    def __init__(self) -> None:
        self.version = CLIENT
        self.seen: dict[str, str | bytes] = {}

    async def GetInfo(
        self,
        request: info_pb2.GetInfoRequest,
        context: grpc.aio.ServicerContext[info_pb2.GetInfoRequest, info_pb2.GetInfoResponse],
    ) -> info_pb2.GetInfoResponse:
        self.seen = dict(context.invocation_metadata() or ())
        if self.seen.get("authorization") != f"Bearer {TOKEN}":
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or unknown token")
        return info_pb2.GetInfoResponse(version=self.version)


class FakeApp:
    def __init__(self, shards: Mapping[int, object], shard_count: int) -> None:
        self.shards = shards
        self.shard_count = shard_count


@pytest.fixture
async def fake_hub() -> AsyncGenerator[tuple[str, FakeInfo]]:
    info = FakeInfo()
    server = grpc.aio.server()
    info_pb2_grpc.add_InfoServiceServicer_to_server(info, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}", info
    finally:
        await server.stop(0)


def config(address: str, **values: str) -> Config:
    return Config.from_env(
        {
            "DISCORD_TOKEN": "discord-token",
            "HUB_ADDRESS": address,
            "HUB_TOKEN": TOKEN,
            "HUB_TLS": "false",
            "OPERATOR_NAME": "Example Operator",
            "OPERATOR_CONTACT": "ops@example.org",
            "PRIVACY_POLICY_URL": "https://example.org/privacy",
            **values,
        }
    )


def unused_address() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{sock.getsockname()[1]}"


async def test_connects_with_the_token(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub

    hub = await connect_hub(config(address))
    await hub.close()

    assert hub.version == CLIENT
    assert info.seen["authorization"] == f"Bearer {TOKEN}"


async def test_close_shuts_the_channel_down(fake_hub: tuple[str, FakeInfo]) -> None:
    hub = await connect_hub(config(fake_hub[0]))

    await hub.close()

    assert hub.channel.get_state() == grpc.ChannelConnectivity.SHUTDOWN


async def test_sends_the_shard_label_of_the_app(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub

    hub = await connect_hub(config(address), shards=FakeApp({2: object(), 0: object()}, 4))
    await hub.close()

    assert info.seen["libre-shards"] == "0,2/4"


async def test_sends_the_configured_shards_without_an_app(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub

    hub = await connect_hub(config(address, SHARD_IDS="2", SHARD_COUNT="4"))
    await hub.close()

    assert info.seen["libre-shards"] == "2/4"


async def test_prefers_the_shards_of_the_app_to_the_configured_ones(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub

    hub = await connect_hub(
        config(address, SHARD_IDS="2", SHARD_COUNT="4"), shards=FakeApp({0: object(), 1: object()}, 2)
    )
    await hub.close()

    assert info.seen["libre-shards"] == "0,1/2"


async def test_sends_no_shard_label_before_the_shards_start(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub

    bot = hikari.GatewayBot("MTIzNDU2Nzg5MDEyMzQ1Njc4.x.y", banner=None)

    hub = await connect_hub(config(address), shards=bot)
    await hub.close()

    assert "libre-shards" not in info.seen


async def test_rejects_a_wrong_token(fake_hub: tuple[str, FakeInfo]) -> None:
    with pytest.raises(HubError, match="HUB_TOKEN"):
        await connect_hub(config(fake_hub[0], HUB_TOKEN="wrong"))


async def test_gives_up_when_the_hub_does_not_answer() -> None:
    address = unused_address()

    with pytest.raises(HubError, match=f"{address}.*0.2 seconds"):
        await connect_hub(config(address), timeout=0.2)


async def test_reports_an_unreadable_ca_file(tmp_path: Path) -> None:
    values = {"HUB_TLS": "true", "HUB_CA_FILE": str(tmp_path / "missing.pem")}

    with pytest.raises(HubError, match="HUB_CA_FILE"):
        await connect_hub(config(unused_address(), **values), timeout=0.2)


@pytest.mark.parametrize(
    "hub_version",
    [
        CLIENT,
        f"{CLIENT_MAJOR}.{CLIENT_MINOR}.99",
        f"{CLIENT_MAJOR}.{CLIENT_MINOR}.0-rc.1",
        f"{CLIENT_MAJOR}.{CLIENT_MINOR + 1}.0",
        f"{CLIENT_MAJOR + 1}.0.0",
    ],
)
async def test_accepts_a_hub_with_the_same_or_a_newer_minor_version(
    fake_hub: tuple[str, FakeInfo], hub_version: str
) -> None:
    address, info = fake_hub
    info.version = hub_version

    hub = await connect_hub(config(address))
    await hub.close()

    assert hub.version == hub_version


async def test_rejects_a_hub_with_an_older_minor_version(fake_hub: tuple[str, FakeInfo]) -> None:
    if (CLIENT_MAJOR, CLIENT_MINOR) == (0, 0):
        pytest.skip("the installed client has no older minor version")
    address, info = fake_hub
    info.version = f"{CLIENT_MAJOR}.{CLIENT_MINOR - 1}.99" if CLIENT_MINOR else f"{CLIENT_MAJOR - 1}.99.0"

    with pytest.raises(HubError, match=info.version):
        await connect_hub(config(address))


async def test_warns_about_a_hub_without_a_version(
    fake_hub: tuple[str, FakeInfo], caplog: pytest.LogCaptureFixture
) -> None:
    address, info = fake_hub
    info.version = "dev"

    with caplog.at_level(logging.WARNING, logger="librebots.kit"):
        hub = await connect_hub(config(address))
    await hub.close()

    assert hub.version == "dev"
    assert "no version" in caplog.text


async def test_rejects_an_unknown_hub_version(fake_hub: tuple[str, FakeInfo]) -> None:
    address, info = fake_hub
    info.version = "banana"

    with pytest.raises(HubError, match="banana"):
        await connect_hub(config(address))
