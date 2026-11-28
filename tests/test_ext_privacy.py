from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, NonCallableMagicMock, create_autospec

import hikari
import lightbulb
import pytest
import risa
from hub_server import serving
from librebots.hub.v1 import privacy_pb2_grpc
from privacy_fakes import FakePrivacy, counts

from librebots.kit import LOCALES, BotInfo, Catalog, Config, Hub, Privacy
from librebots.kit.ext import privacy as privacy_ext

EXTENSION = "librebots.kit.ext.privacy"
USER = 123456789012345678
OWNER = 323456789012345678
GUILD = 987654321098765432
STARBOARD = BotInfo(
    name="LibreStarboard",
    hub_name="starboard",
    version="1.2.0",
    accent_color=0xEF9F27,
    source_url="https://github.com/libre-bots/starboard",
)
BOT_TEXTS = {
    "en-US": """
bot-description = Pins the best messages of a server.
privacy-category-starboard-entries = { $count ->
    [one] { $count } starboard entry
   *[other] { $count } starboard entries
}
privacy-category-starboard-stars = { $count } stars
privacy-category-starboard-settings = { $count } set of starboard settings
""",
    "de": """
bot-description = Heftet die besten Nachrichten eines Servers an.
privacy-category-starboard-entries = { $count } Starboard-Einträge
privacy-category-starboard-stars = { $count } Sterne
privacy-category-starboard-settings = { $count } Starboard-Einstellungen
""",
}


def config() -> Config:
    return Config.from_env(
        {
            "DISCORD_TOKEN": "discord-token",
            "HUB_ADDRESS": "127.0.0.1:1",
            "HUB_TOKEN": "hub-token",
            "HUB_TLS": "false",
            "OPERATOR_NAME": "Example Operator",
            "OPERATOR_CONTACT": "ops@example.org",
            "PRIVACY_POLICY_URL": "https://example.org/privacy",
        }
    )


class Person:
    def __init__(self, user_id: int, username: str, *, guild_owner: int | None = OWNER) -> None:
        user = create_autospec(hikari.User, instance=True)
        user.id = hikari.Snowflake(user_id)
        user.username = username
        self.user: hikari.User = user
        self.guild: hikari.RESTGuild | None = None
        if guild_owner is not None:
            guild = create_autospec(hikari.RESTGuild, instance=True)
            guild.id = hikari.Snowflake(GUILD)
            guild.name = "Star Gazers"
            guild.owner_id = hikari.Snowflake(guild_owner)
            self.guild = guild

    def act(self, interaction: NonCallableMagicMock, locale: hikari.Locale) -> None:
        interaction.user = self.user
        interaction.guild_id = None if self.guild is None else self.guild.id
        interaction.locale = locale
        interaction.get_guild.return_value = None
        interaction.fetch_guild.return_value = self.guild


MEMBER = Person(USER, "christopher")
OWNER_PERSON = Person(OWNER, "olivia")
IN_DMS = Person(USER, "christopher", guild_owner=None)


class Screen:
    def __init__(self, components: Sequence[hikari.api.ComponentBuilder]) -> None:
        (container,) = components
        payload, self.files = container.build()
        self.accent = payload.get("accent_color")
        self.texts: list[str] = []
        self.buttons: dict[str, dict[str, Any]] = {}
        for child in payload["components"]:
            if child["type"] == hikari.ComponentType.TEXT_DISPLAY:
                self.texts.append(child["content"])
            elif child["type"] == hikari.ComponentType.ACTION_ROW:
                self.buttons.update({button["label"]: button for button in child["components"]})


class Modal:
    def __init__(self, interaction: NonCallableMagicMock) -> None:
        call = interaction.create_modal_response.await_args
        assert call is not None
        self.title, self.custom_id = call.args[:2]
        (row,) = call.kwargs["components"]
        payload, _ = row.build()
        (self.field,) = payload["components"]


class Bot:
    def __init__(self, hub: Hub, tmp_path: Path) -> None:
        for locale, texts in BOT_TEXTS.items():
            (tmp_path / locale).mkdir()
            (tmp_path / locale / "bot.ftl").write_text(texts, encoding="utf-8")
        catalog = Catalog.load(LOCALES, tmp_path)
        app = create_autospec(hikari.GatewayBot, instance=True)
        app.rest = self.rest = create_autospec(hikari.api.RESTClient, instance=True)
        app.event_manager = self.events = hikari.impl.EventManagerImpl(
            hikari.impl.EntityFactoryImpl(app), hikari.impl.EventFactoryImpl(app), hikari.Intents.NONE
        )
        self.app: hikari.GatewayBot = app
        self.client = lightbulb.client_from_app(
            self.app, localization_provider=catalog.localization_provider, sync_commands=False
        )
        risa.client_from_lightbulb(self.client)
        self.privacy = Privacy(hub)
        registry = self.client.di.registry_for(lightbulb.di.Contexts.DEFAULT)
        registry.register_value(Config, config())
        registry.register_value(Catalog, catalog)
        registry.register_value(BotInfo, STARBOARD)
        registry.register_value(Privacy, self.privacy)

    async def start(self) -> None:
        await self.client.load_extensions(EXTENSION)
        await self.client.start()
        await self.events.dispatch(hikari.StartedEvent(app=self.app), return_tasks=True)

    async def stop(self) -> None:
        await self.privacy.close()
        # Unloading drops the module, and importing it again would register its risa view a second time.
        await self.client.stop()

    async def run_privacy(self, person: Person, locale: hikari.Locale = hikari.Locale.EN_US) -> Screen:
        interaction = create_autospec(hikari.CommandInteraction, instance=True)
        interaction.command_name = "privacy"
        interaction.command_type = hikari.CommandType.SLASH
        interaction.options = []
        interaction.registered_guild_id = None
        person.act(interaction, locale)
        response: AsyncMock = interaction.create_initial_response
        await self.client.handle_interaction_create(interaction)
        call = response.await_args
        assert call is not None
        assert call.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
        return Screen(call.kwargs["components"])

    async def click(self, screen: Screen, label: str, person: Person) -> NonCallableMagicMock:
        interaction = create_autospec(hikari.ComponentInteraction, instance=True)
        interaction.custom_id = screen.buttons[label]["custom_id"]
        person.act(interaction, hikari.Locale.EN_US)
        self.rest.create_interaction_response.reset_mock()
        shard = create_autospec(hikari.api.GatewayShard, instance=True)
        await self.events.dispatch(
            hikari.ComponentInteractionCreateEvent(shard=shard, interaction=interaction), return_tasks=True
        )
        return interaction

    def answer(self) -> tuple[hikari.ResponseType, Screen, hikari.UndefinedOr[hikari.MessageFlag]]:
        call = self.rest.create_interaction_response.await_args
        assert call is not None
        response_type, flags = call.args[2], call.kwargs.get("flags", hikari.UNDEFINED)
        assert isinstance(response_type, hikari.ResponseType)
        assert flags is hikari.UNDEFINED or isinstance(flags, hikari.MessageFlag)
        return response_type, Screen(call.kwargs["components"]), flags

    async def submit(
        self, modal: Modal, typed: str, person: Person, *, record_in: list[str] | None = None
    ) -> tuple[Literal["edited", "private reply"], Screen]:
        interaction = create_autospec(hikari.ModalInteraction, instance=True)
        interaction.custom_id = modal.custom_id
        text_input = hikari.TextInputComponent(
            type=hikari.ComponentType.TEXT_INPUT, id=2, custom_id=modal.field["custom_id"], value=typed
        )
        interaction.components = [
            hikari.ModalActionRowComponent(type=hikari.ComponentType.ACTION_ROW, id=1, components=[text_input])
        ]
        person.act(interaction, hikari.Locale.EN_US)
        response: AsyncMock = interaction.create_initial_response
        edit: AsyncMock = interaction.edit_initial_response
        if record_in is not None:

            def record(response_type: hikari.ResponseType, *args: object, **kwargs: object) -> None:
                record_in.append(f"answered {response_type.name}")

            response.side_effect = record
        shard = create_autospec(hikari.api.GatewayShard, instance=True)
        await self.events.dispatch(
            hikari.ModalInteractionCreateEvent(shard=shard, interaction=interaction), return_tasks=True
        )
        (first,) = response.await_args_list
        if first.args[0] == hikari.ResponseType.DEFERRED_MESSAGE_UPDATE:
            edited = edit.await_args
            assert edited is not None
            return "edited", Screen(edited.kwargs["components"])
        assert first.args[0] == hikari.ResponseType.MESSAGE_CREATE
        assert first.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
        return "private reply", Screen(first.kwargs["components"])


@pytest.fixture
async def hub() -> AsyncGenerator[tuple[Hub, FakePrivacy]]:
    fake = FakePrivacy()
    async with serving(lambda server: privacy_pb2_grpc.add_PrivacyServiceServicer_to_server(fake, server)) as hub:
        yield hub, fake


@pytest.fixture
async def bot(hub: tuple[Hub, FakePrivacy], tmp_path: Path) -> AsyncGenerator[Bot]:
    bot = Bot(hub[0], tmp_path)
    await bot.start()
    yield bot
    await bot.stop()


async def test_shows_who_runs_the_bot_and_what_it_stores(bot: Bot) -> None:
    screen = await bot.run_privacy(MEMBER)

    assert screen.accent == hikari.Color(0xEF9F27)
    assert screen.texts == [
        "### Your data",
        "Example Operator (ops@example.org) runs this instance of LibreStarboard.",
        "It stores about you:\n- 12 starboard entries\n- 87 stars",
    ]
    assert list(screen.buttons) == ["Export my data", "Delete my data", "Privacy policy"]
    assert screen.buttons["Privacy policy"]["url"] == "https://example.org/privacy"


async def test_greys_out_export_and_deletion_when_nothing_is_stored(bot: Bot, hub: tuple[Hub, FakePrivacy]) -> None:
    hub[1].user_data = []

    screen = await bot.run_privacy(MEMBER)

    assert screen.texts[2] == "It stores nothing about you."
    assert screen.buttons["Export my data"]["disabled"] is True
    assert screen.buttons["Delete my data"]["disabled"] is True


@pytest.mark.parametrize(("person", "offered"), [(OWNER_PERSON, True), (MEMBER, False), (IN_DMS, False)])
async def test_offers_deleting_the_server_data_to_its_owner_only(bot: Bot, person: Person, offered: bool) -> None:
    screen = await bot.run_privacy(person)

    assert ("Delete this server's data" in screen.buttons) is offered


async def test_exports_the_data_as_a_file(bot: Bot) -> None:
    screen = await bot.run_privacy(MEMBER)

    await bot.click(screen, "Export my data", MEMBER)

    response_type, export, flags = bot.answer()
    assert (response_type, flags) == (hikari.ResponseType.MESSAGE_CREATE, hikari.MessageFlag.EPHEMERAL)
    assert export.texts == ["### Your data export", "This file holds everything LibreStarboard stores about you."]
    (file,) = export.files
    assert file.filename == "starboard-data.json"
    assert f'"user_id":"{USER}"' in (await file.read()).decode()


async def test_deletes_my_data_once_i_type_my_username(bot: Bot, hub: tuple[Hub, FakePrivacy]) -> None:
    overview = await bot.run_privacy(MEMBER)

    await bot.click(overview, "Delete my data", MEMBER)
    response_type, warning, _ = bot.answer()
    assert response_type == hikari.ResponseType.MESSAGE_UPDATE
    assert warning.accent == hikari.Color(0xE24B4A)
    assert warning.texts == [
        "### Delete everything about you?",
        "This deletes everything LibreStarboard stores about you, in every server:\n- 12 starboard entries\n- 87 stars",
        "It cannot be undone. Export your data first if you want a copy.",
    ]
    assert warning.buttons["Delete everything"]["style"] == hikari.ButtonStyle.DANGER

    modal = Modal(await bot.click(warning, "Delete everything", MEMBER))
    assert modal.title == "Delete all your data"
    assert (modal.field["label"], modal.field["placeholder"]) == ("Type your username to confirm", "christopher")

    answer, done = await bot.submit(modal, "  Christopher ", MEMBER, record_in=hub[1].log)
    await bot.privacy.wait_for_deletions()

    assert answer == "edited"
    assert done.texts == [
        "### Deletion started",
        "Your data is being deleted in the background. This can take a few minutes.",
    ]
    assert hub[1].log[-3:] == ["answered DEFERRED_MESSAGE_UPDATE", f"request deletion {USER}", f"delete user {USER}"]


async def test_keeps_my_data_when_the_username_does_not_match(bot: Bot, hub: tuple[Hub, FakePrivacy]) -> None:
    overview = await bot.run_privacy(MEMBER)
    await bot.click(overview, "Delete my data", MEMBER)
    _, warning, _ = bot.answer()
    modal = Modal(await bot.click(warning, "Delete everything", MEMBER))

    answer, refusal = await bot.submit(modal, "someone-else", MEMBER)

    assert answer == "private reply"
    assert refusal.texts == ["### Nothing was deleted", "That is not your username, so nothing was deleted."]
    assert not [entry for entry in hub[1].log if entry.startswith(("request deletion", "delete user"))]


async def test_goes_back_to_the_overview(bot: Bot) -> None:
    overview = await bot.run_privacy(MEMBER)
    await bot.click(overview, "Delete my data", MEMBER)
    _, warning, _ = bot.answer()

    await bot.click(warning, "Cancel", MEMBER)

    response_type, back, _ = bot.answer()
    assert response_type == hikari.ResponseType.MESSAGE_UPDATE
    assert back.texts == overview.texts


async def test_deletes_the_server_data_once_its_owner_types_its_name(bot: Bot, hub: tuple[Hub, FakePrivacy]) -> None:
    overview = await bot.run_privacy(OWNER_PERSON)

    await bot.click(overview, "Delete this server's data", OWNER_PERSON)
    _, warning, _ = bot.answer()
    assert warning.texts == [
        "### Delete this server's data?",
        "This deletes everything LibreStarboard stores about this server:\n"
        "- 1 set of starboard settings\n- 342 starboard entries",
        "It cannot be undone. The bot's messages in this server stay.",
    ]

    modal = Modal(await bot.click(warning, "Delete server data", OWNER_PERSON))
    assert modal.title == "Delete this server's data"
    assert (modal.field["label"], modal.field["placeholder"]) == ("Type the server name to confirm", "Star Gazers")

    answer, done = await bot.submit(modal, "star gazers", OWNER_PERSON, record_in=hub[1].log)

    assert answer == "edited"
    assert done.texts == [
        "### Server data deleted",
        "LibreStarboard deleted:\n- 1 set of starboard settings\n- 342 starboard entries",
    ]
    assert hub[1].log[-2:] == ["answered DEFERRED_MESSAGE_UPDATE", f"delete guild {GUILD}"]


async def test_lets_only_the_owner_delete_the_server_data(bot: Bot, hub: tuple[Hub, FakePrivacy]) -> None:
    overview = await bot.run_privacy(OWNER_PERSON)
    await bot.click(overview, "Delete this server's data", OWNER_PERSON)
    _, warning, _ = bot.answer()
    modal = Modal(await bot.click(warning, "Delete server data", OWNER_PERSON))
    handed_over = Person(OWNER, "olivia", guild_owner=USER)

    await bot.click(overview, "Delete this server's data", handed_over)
    _, after_click, flags = bot.answer()
    answer, after_submit = await bot.submit(modal, "Star Gazers", handed_over)

    only_owner = ["### Nothing was deleted", "Only the server owner can delete this server's data."]
    assert (after_click.texts, flags) == (only_owner, hikari.MessageFlag.EPHEMERAL)
    assert (answer, after_submit.texts) == ("private reply", only_owner)
    assert f"delete guild {GUILD}" not in hub[1].log


async def test_names_a_category_the_bot_has_no_text_for(
    bot: Bot, hub: tuple[Hub, FakePrivacy], caplog: pytest.LogCaptureFixture
) -> None:
    hub[1].user_data = counts(starboard_extras=3)

    with caplog.at_level(logging.WARNING, logger=privacy_ext.__name__):
        screen = await bot.run_privacy(MEMBER)

    assert screen.texts[2] == "It stores about you:\n- starboard-extras: 3"
    assert "privacy-category-starboard-extras" in caplog.text


@pytest.mark.parametrize(
    ("person", "button", "confirm", "typed", "failing"),
    [
        (MEMBER, "Delete my data", "Delete everything", "christopher", "request deletion"),
        (OWNER_PERSON, "Delete this server's data", "Delete server data", "Star Gazers", "delete guild"),
    ],
)
async def test_does_not_claim_an_outcome_when_the_hub_fails(
    bot: Bot, hub: tuple[Hub, FakePrivacy], person: Person, button: str, confirm: str, typed: str, failing: str
) -> None:
    overview = await bot.run_privacy(person)
    await bot.click(overview, button, person)
    _, warning, _ = bot.answer()
    modal = Modal(await bot.click(warning, confirm, person))
    hub[1].failing.add(failing)

    answer, unconfirmed = await bot.submit(modal, typed, person)

    assert answer == "edited"
    assert unconfirmed.accent == hikari.Color(0xE24B4A)
    assert unconfirmed.texts == [
        "### Deletion not confirmed",
        "I could not reach my data to finish this, so I cannot tell whether it was deleted. "
        "Run /privacy again in a minute to check.",
    ]


async def test_answers_in_the_language_of_the_caller(bot: Bot) -> None:
    screen = await bot.run_privacy(MEMBER, hikari.Locale.DE)

    assert screen.texts == [
        "### Deine Daten",
        "Example Operator (ops@example.org) betreibt diese Instanz von LibreStarboard.",
        "Über dich gespeichert:\n- 12 Starboard-Einträge\n- 87 Sterne",
    ]
    assert list(screen.buttons) == ["Meine Daten exportieren", "Meine Daten löschen", "Datenschutzerklärung"]


async def test_continues_pending_deletions_after_the_start(hub: tuple[Hub, FakePrivacy], tmp_path: Path) -> None:
    hub[1].pending.append(USER)
    bot = Bot(hub[0], tmp_path)

    await bot.start()
    await bot.privacy.wait_for_deletions()
    await bot.stop()

    assert f"delete user {USER}" in hub[1].log
