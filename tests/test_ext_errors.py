from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from unittest.mock import AsyncMock, create_autospec

import grpc
import hikari
import lightbulb
import pytest
import risa
from risa import ui

from librebots.kit import LOCALES, Catalog, HubRequestError, HubUnavailableError
from librebots.kit.ext import errors

EXTENSION = "librebots.kit.ext.errors"
RED = hikari.Color(0xE24B4A)


TOKEN = "aW50ZXJhY3Rpb246c2VjcmV0"
TOKEN_URL = f"https://discord.com/api/v10/webhooks/123/{TOKEN}/messages/@original"


def forbidden() -> hikari.ForbiddenError:
    return hikari.ForbiddenError(url="https://discord.com/api", headers={}, raw_body="")


FAILURES: dict[str, Callable[[], Exception]] = {
    "hub-down": lambda: HubUnavailableError("GetStats failed with UNAVAILABLE: hub is down"),
    "forbidden": forbidden,
    "refused": lambda: HubRequestError(
        "GuildJoined failed with NOT_FOUND", code=grpc.StatusCode.NOT_FOUND, reason=None
    ),
    "broken": lambda: RuntimeError("it broke"),
    "webhook-gone": lambda: hikari.NotFoundError(url=TOKEN_URL, headers={}, raw_body="", message="Unknown Webhook"),
}


def failing_command(name: str) -> type[lightbulb.SlashCommand]:
    class Failing(lightbulb.SlashCommand, name=name, description="Fails"):
        @lightbulb.invoke
        async def invoke(self, ctx: lightbulb.Context) -> None:
            raise FAILURES[name]()

    return Failing


@risa.register(name="kit-test-failing")
class Failing(risa.View):
    def render(self) -> ui.Layout:
        return ui.Row(ui.Button(self.hub_down, label="Hub"), ui.Button(self.broken, label="Broken"))

    @risa.handler
    async def hub_down(self, ctx: risa.ComponentContext) -> None:
        raise FAILURES["hub-down"]()

    @risa.handler
    async def broken(self, ctx: risa.ComponentContext) -> None:
        raise FAILURES["broken"]()


class Command:
    def __init__(self, name: str, locale: hikari.Locale = hikari.Locale.EN_US) -> None:
        interaction = create_autospec(hikari.CommandInteraction, instance=True)
        interaction.command_name = name
        interaction.command_type = hikari.CommandType.SLASH
        interaction.options = []
        interaction.registered_guild_id = None
        interaction.locale = locale
        self.interaction: hikari.CommandInteraction = interaction
        self.response: AsyncMock = interaction.create_initial_response


class Bot:
    def __init__(self) -> None:
        app = create_autospec(hikari.GatewayBot, instance=True)
        app.rest = self.rest = create_autospec(hikari.api.RESTClient, instance=True)
        app.event_manager = self.events = hikari.impl.EventManagerImpl(
            hikari.impl.EntityFactoryImpl(app), hikari.impl.EventFactoryImpl(app), hikari.Intents.NONE
        )
        self.app: hikari.GatewayBot = app
        self.client = lightbulb.client_from_app(self.app, sync_commands=False)
        self.views = risa.client_from_lightbulb(self.client)
        self.views.add_view(Failing)
        self.client.di.registry_for(lightbulb.di.Contexts.DEFAULT).register_value(Catalog, Catalog.load(LOCALES))
        for name in FAILURES:
            self.client.register(failing_command(name))

    async def start(self) -> None:
        await self.client.load_extensions(EXTENSION)
        await self.client.start()
        await self.events.dispatch(hikari.StartedEvent(app=self.app), return_tasks=True)

    async def run(self, command: Command) -> AsyncMock:
        await self.client.handle_interaction_create(command.interaction)
        return command.response

    async def click(self, button: int, locale: hikari.Locale = hikari.Locale.EN_US) -> AsyncMock:
        rendered = await self.views.build(Failing())
        row, _ = rendered.components[0].build()
        interaction = create_autospec(hikari.ComponentInteraction, instance=True)
        interaction.custom_id = row["components"][button]["custom_id"]
        interaction.locale = locale
        shard = create_autospec(hikari.api.GatewayShard, instance=True)
        event = hikari.ComponentInteractionCreateEvent(shard=shard, interaction=interaction)
        await self.events.dispatch(event, return_tasks=True)
        return self.rest.create_interaction_response


@pytest.fixture
async def bot() -> AsyncGenerator[Bot]:
    bot = Bot()
    await bot.start()
    yield bot
    await bot.client.stop()
    await bot.client.unload_extensions(EXTENSION)


def reply(response: AsyncMock) -> tuple[hikari.Color, list[str]]:
    call = response.await_args
    assert call is not None
    assert call.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    components: Sequence[hikari.api.ComponentBuilder] = call.kwargs["components"]
    (container,) = components
    payload, _ = container.build()
    return payload["accent_color"], [child["content"] for child in payload["components"]]


def reference_in(text: str) -> str:
    match = re.search(r"`([0-9a-f]{6})`", text)
    assert match is not None, text
    return match[1]


async def test_tells_the_caller_the_hub_is_unreachable(bot: Bot, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=errors.__name__):
        response = await bot.run(Command("hub-down"))

    assert reply(response) == (
        RED,
        ["### Not reachable right now", "I can't reach my data at the moment. Try again in a minute."],
    )
    assert "hub is down" in caplog.text


async def test_tells_the_caller_a_permission_is_missing(bot: Bot) -> None:
    _, texts = reply(await bot.run(Command("forbidden")))

    assert texts[0] == "### Missing permission"


@pytest.mark.parametrize(
    ("name", "logged"),
    [
        ("broken", "RuntimeError: it broke"),
        ("refused", "HubRequestError: GuildJoined failed with NOT_FOUND"),
    ],
)
async def test_gives_a_reference_that_the_log_repeats(
    bot: Bot, caplog: pytest.LogCaptureFixture, name: str, logged: str
) -> None:
    with caplog.at_level(logging.ERROR, logger=errors.__name__):
        response = await bot.run(Command(name))

    _, texts = reply(response)
    assert texts[0] == "### Something went wrong"
    (record,) = caplog.records
    assert reference_in(texts[1]) in record.getMessage()
    assert "Traceback" in record.getMessage()
    assert logged in record.getMessage()


async def test_logs_no_interaction_token(bot: Bot, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=errors.__name__):
        await bot.run(Command("webhook-gone"))

    assert "Unknown Webhook" in caplog.text
    assert "/webhooks/123/***/messages" in caplog.text
    assert TOKEN not in caplog.text


async def test_answers_in_the_language_of_the_caller(bot: Bot) -> None:
    _, texts = reply(await bot.run(Command("hub-down", hikari.Locale.DE)))

    assert texts[0] == "### Gerade nicht erreichbar"


async def test_leaves_errors_to_the_handlers_of_the_bot(bot: Bot) -> None:
    handled: list[Exception] = []

    async def handle(exc: lightbulb.exceptions.ExecutionPipelineFailedException) -> bool:
        handled.extend(exc.causes)
        return True

    bot.client.error_handler(handle)
    response = await bot.run(Command("broken"))

    assert [repr(cause) for cause in handled] == [repr(FAILURES["broken"]())]
    response.assert_not_awaited()


async def test_logs_an_answer_that_fails(bot: Bot, caplog: pytest.LogCaptureFixture) -> None:
    command = Command("hub-down")
    command.response.side_effect = hikari.NotFoundError(
        url=TOKEN_URL, headers={}, raw_body="", message="Unknown interaction"
    )

    with caplog.at_level(logging.WARNING, logger=errors.__name__):
        await bot.run(command)

    assert "Unknown interaction" in caplog.text
    assert TOKEN not in caplog.text


async def test_tells_the_clicker_the_hub_is_unreachable(bot: Bot) -> None:
    response = await bot.click(0, hikari.Locale.DE)

    assert reply(response) == (
        RED,
        [
            "### Gerade nicht erreichbar",
            "Ich komme gerade nicht an meine Daten. Versuch es in einer Minute noch einmal.",
        ],
    )


async def test_gives_a_clicker_a_reference_that_the_log_repeats(bot: Bot, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=errors.__name__):
        response = await bot.click(1)

    _, texts = reply(response)
    (record,) = caplog.records
    assert reference_in(texts[1]) in record.getMessage()
    assert "RuntimeError: it broke" in record.getMessage()
