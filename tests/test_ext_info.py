from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable
from pathlib import Path
from types import TracebackType
from typing import Self
from unittest.mock import AsyncMock, create_autospec

import grpc
import grpc.aio
import hikari
import lightbulb
import pytest
from hub_server import serving
from librebots.hub.v1 import stats_pb2, stats_pb2_grpc

from librebots.kit import LOCALES, BotInfo, Catalog, Config, Hub
from librebots.kit.ext import info

EXTENSION = "librebots.kit.ext.info"
STARBOARD = BotInfo(
    name="LibreStarboard",
    hub_name="starboard",
    version="1.2.0",
    accent_color=0xEF9F27,
    source_url="https://github.com/libre-bots/starboard",
)
SOURCE = ("Source code", "https://github.com/libre-bots/starboard")
SUPPORT = ("Support", "https://example.org/support")
POLICY = ("Privacy policy", "https://example.org/privacy")
PRIVACY = "-# Use /privacy to see, export or delete what this bot stores about you."


class FakeStats(stats_pb2_grpc.StatsServiceServicer):
    def __init__(self) -> None:
        self.guilds = {"starboard": 1234, "tickets": 7}
        self.failing = False

    async def GetStats(
        self,
        request: stats_pb2.GetStatsRequest,
        context: grpc.aio.ServicerContext[stats_pb2.GetStatsRequest, Awaitable[stats_pb2.GetStatsResponse]],
    ) -> stats_pb2.GetStatsResponse:
        if self.failing:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "hub is down")
        bots = [
            stats_pb2.BotFigures(bot=bot, figures=stats_pb2.Figures(guild_count=count))
            for bot, count in self.guilds.items()
        ]
        return stats_pb2.GetStatsResponse(bots=bots)


@pytest.fixture
async def stats() -> AsyncGenerator[tuple[Hub, FakeStats]]:
    fake = FakeStats()
    async with serving(lambda server: stats_pb2_grpc.add_StatsServiceServicer_to_server(fake, server)) as hub:
        yield hub, fake


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    for locale, description in [
        ("en-US", "Pins the best messages of a server."),
        ("de", "Heftet die besten Nachrichten eines Servers an."),
    ]:
        folder = tmp_path / locale
        folder.mkdir()
        (folder / "bot.ftl").write_text(f"bot-description = {description}\n", encoding="utf-8")
    return Catalog.load(LOCALES, tmp_path)


def config(**values: str) -> Config:
    return Config.from_env(
        {
            "DISCORD_TOKEN": "discord-token",
            "HUB_ADDRESS": "127.0.0.1:1",
            "HUB_TOKEN": "hub-token",
            "HUB_TLS": "false",
            "OPERATOR_NAME": "Example Operator",
            "OPERATOR_CONTACT": "ops@example.org",
            "PRIVACY_POLICY_URL": "https://example.org/privacy",
            **values,
        }
    )


class InfoBot:
    def __init__(self, hub: Hub, catalog: Catalog, settings: Config) -> None:
        app: hikari.GatewayBot = create_autospec(hikari.GatewayBot, instance=True)
        self.client = lightbulb.client_from_app(
            app, localization_provider=catalog.localization_provider, sync_commands=False
        )
        registry = self.client.di.registry_for(lightbulb.di.Contexts.DEFAULT)
        registry.register_value(Config, settings)
        registry.register_value(Catalog, catalog)
        registry.register_value(Hub, hub)
        registry.register_value(BotInfo, STARBOARD)

    async def __aenter__(self) -> Self:
        await self.client.load_extensions(EXTENSION)
        await self.client.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.client.stop()
        await self.client.unload_extensions(EXTENSION)

    async def info(
        self, locale: hikari.Locale = hikari.Locale.EN_US
    ) -> tuple[hikari.Color, list[str], list[tuple[str, str]]]:
        interaction = create_autospec(hikari.CommandInteraction, instance=True)
        interaction.command_name = "info"
        interaction.command_type = hikari.CommandType.SLASH
        interaction.options = []
        interaction.registered_guild_id = None
        interaction.locale = locale
        response: AsyncMock = interaction.create_initial_response
        await self.client.handle_interaction_create(interaction)

        call = response.await_args
        assert call is not None
        assert call.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
        (container,) = call.kwargs["components"]
        payload, _ = container.build()
        children = payload["components"]
        texts = [child["content"] for child in children if child["type"] == hikari.ComponentType.TEXT_DISPLAY]
        rows = [child for child in children if child["type"] == hikari.ComponentType.ACTION_ROW]
        buttons = [(button["label"], button["url"]) for row in rows for button in row["components"]]
        return payload["accent_color"], texts, buttons


async def test_shows_the_bot_its_operator_and_links(stats: tuple[Hub, FakeStats], catalog: Catalog) -> None:
    async with InfoBot(stats[0], catalog, config(SUPPORT_URL="https://example.org/support")) as bot:
        shown = await bot.info()

    assert shown == (
        hikari.Color(0xEF9F27),
        [
            "### LibreStarboard",
            "Pins the best messages of a server.",
            f"-# Version 1.2.0 · 1,234 servers · Run by Example Operator (ops@example.org)\n{PRIVACY}",
        ],
        [SOURCE, SUPPORT, POLICY],
    )


async def test_leaves_out_the_support_link_without_a_support_url(
    stats: tuple[Hub, FakeStats], catalog: Catalog
) -> None:
    async with InfoBot(stats[0], catalog, config()) as bot:
        _, _, buttons = await bot.info()

    assert buttons == [SOURCE, POLICY]


async def test_counts_one_server_in_the_singular(stats: tuple[Hub, FakeStats], catalog: Catalog) -> None:
    stats[1].guilds["starboard"] = 1

    async with InfoBot(stats[0], catalog, config()) as bot:
        _, texts, _ = await bot.info()

    assert texts[2].startswith("-# Version 1.2.0 · 1 server · ")


@pytest.mark.parametrize("cause", ["hub down", "bot unknown to the hub"])
async def test_shows_no_server_count_without_one_from_the_hub(
    stats: tuple[Hub, FakeStats], catalog: Catalog, caplog: pytest.LogCaptureFixture, cause: str
) -> None:
    if cause == "hub down":
        stats[1].failing = True
    else:
        del stats[1].guilds["starboard"]

    with caplog.at_level(logging.WARNING, logger=info.__name__):
        async with InfoBot(stats[0], catalog, config()) as bot:
            _, texts, _ = await bot.info()

    assert texts[2] == f"-# Version 1.2.0 · Run by Example Operator (ops@example.org)\n{PRIVACY}"
    assert "server count" in caplog.text


async def test_answers_in_the_language_of_the_caller(stats: tuple[Hub, FakeStats], catalog: Catalog) -> None:
    async with InfoBot(stats[0], catalog, config()) as bot:
        _, texts, buttons = await bot.info(hikari.Locale.DE)

    assert texts[1:] == [
        "Heftet die besten Nachrichten eines Servers an.",
        "-# Version 1.2.0 · 1.234 Server · Betrieben von Example Operator (ops@example.org)\n"
        "-# Mit /privacy siehst, exportierst oder löschst du, was dieser Bot über dich speichert.",
    ]
    assert [label for label, _ in buttons] == ["Quellcode", "Datenschutzerklärung"]
