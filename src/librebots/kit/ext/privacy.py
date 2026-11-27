"""Adds `/privacy`: who runs the bot, what it stores about the caller, and export and deletion of that data.

Deleting asks for the person's username, or for the server's name from the server owner, typed into a modal. Needs
`Privacy`, `Config`, `BotInfo`, a risa client and a `Catalog` with kit's texts and the bot's `privacy-category-<name>`
texts in lightbulb's dependency injection.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import hikari
import lightbulb
import msgspec
import risa
from risa import ui

from librebots.kit.bot import BotInfo
from librebots.kit.config import Config
from librebots.kit.hub import HubError
from librebots.kit.i18n import Catalog
from librebots.kit.privacy import Privacy

__all__ = ["loader"]

_LOGGER = logging.getLogger(__name__)
_RED = 0xE24B4A
_DELETE_USER_MODAL = "kit-privacy-delete-user"
_DELETE_SERVER_MODAL = "kit-privacy-delete-server"
_TYPED_NAME = "typed-name"

loader = lightbulb.Loader()


@risa.register(name="kit-privacy")
class _PrivacyOverview(risa.View):
    locale: risa.Prop[str] = "en-US"
    stored: risa.Prop[dict[str, int]] = msgspec.field(default_factory=dict[str, int])
    server_owner: risa.Prop[bool] = False
    catalog: risa.Prop[Catalog | None] = None
    config: risa.Prop[Config | None] = None
    bot: risa.Prop[BotInfo | None] = None

    def render(self) -> ui.Layout:
        if self.catalog is None or self.config is None or self.bot is None:
            raise RuntimeError("the overview renders only with its texts, config and bot info set")
        catalog, locale = self.catalog, self.locale
        if self.stored:
            stored = f"{catalog.text(locale, 'kit-privacy-overview.stored')}\n{_listed(catalog, locale, self.stored)}"
        else:
            stored = catalog.text(locale, "kit-privacy-overview.nothing")
        buttons: list[ui.Button | ui.LinkButton] = [
            ui.Button(
                self.export_my_data,
                label=catalog.text(locale, "kit-privacy-export.button"),
                style=hikari.ButtonStyle.SECONDARY,
                disabled=not self.stored,
            ),
            ui.Button(
                self.ask_to_delete_my_data,
                label=catalog.text(locale, "kit-privacy-delete.button"),
                style=hikari.ButtonStyle.DANGER,
                disabled=not self.stored,
            ),
            ui.LinkButton(self.config.privacy_policy_url, label=catalog.text(locale, "kit-privacy-overview.policy")),
        ]
        if self.server_owner:
            buttons.append(
                ui.Button(
                    self.ask_to_delete_server_data,
                    label=catalog.text(locale, "kit-privacy-server.button"),
                    style=hikari.ButtonStyle.DANGER,
                )
            )
        operator = catalog.text(
            locale,
            "kit-privacy-overview",
            operator=self.config.operator_name,
            contact=self.config.operator_contact,
            bot=self.bot.name,
        )
        return ui.Container(
            ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-overview.title')}"),
            ui.TextDisplay(operator),
            ui.TextDisplay(stored),
            ui.Row(*buttons),
            accent_color=self.bot.accent_color,
        )

    @risa.handler
    async def export_my_data(
        self,
        ctx: risa.ComponentContext,
        privacy: Privacy = risa.INJECTED,
        catalog: Catalog = risa.INJECTED,
        bot: BotInfo = risa.INJECTED,
    ) -> None:
        locale = ctx.interaction.locale
        exported = await privacy.export_user(ctx.user.id)
        layout = ui.Container(
            ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-export.title')}"),
            ui.TextDisplay(catalog.text(locale, "kit-privacy-export", bot=bot.name)),
            ui.File(hikari.Bytes(exported.encode(), f"{bot.hub_name}-data.json")),
            accent_color=bot.accent_color,
        )
        await ctx.respond(**ui.render(layout), ephemeral=True)

    @risa.handler
    async def ask_to_delete_my_data(
        self,
        ctx: risa.ComponentContext,
        privacy: Privacy = risa.INJECTED,
        catalog: Catalog = risa.INJECTED,
        bot: BotInfo = risa.INJECTED,
    ) -> None:
        locale = ctx.interaction.locale
        stored = await privacy.count_user(ctx.user.id)
        what = catalog.text(locale, "kit-privacy-delete", bot=bot.name)
        layout = ui.Container(
            ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-delete.title')}"),
            ui.TextDisplay(f"{what}\n{_listed(catalog, locale, stored)}"),
            ui.TextDisplay(catalog.text(locale, "kit-privacy-delete.warning")),
            ui.Row(
                ui.Button(
                    self.open_delete_my_data_modal,
                    label=catalog.text(locale, "kit-privacy-delete.confirm"),
                    style=hikari.ButtonStyle.DANGER,
                ),
                ui.Button(
                    self.back_to_overview,
                    label=catalog.text(locale, "kit-privacy-cancel"),
                    style=hikari.ButtonStyle.SECONDARY,
                ),
            ),
            accent_color=_RED,
        )
        await ctx.edit(layout)

    @risa.handler
    async def open_delete_my_data_modal(self, ctx: risa.ComponentContext, catalog: Catalog = risa.INJECTED) -> None:
        locale = ctx.interaction.locale
        field = hikari.impl.ModalActionRowBuilder().add_text_input(
            _TYPED_NAME,
            catalog.text(locale, "kit-privacy-delete.input"),
            placeholder=ctx.user.username,
            max_length=100,
        )
        title = catalog.text(locale, "kit-privacy-delete.modal")
        await ctx.interaction.create_modal_response(title, _DELETE_USER_MODAL, components=[field])

    @risa.handler
    async def ask_to_delete_server_data(
        self,
        ctx: risa.ComponentContext,
        privacy: Privacy = risa.INJECTED,
        catalog: Catalog = risa.INJECTED,
        bot: BotInfo = risa.INJECTED,
    ) -> None:
        locale = ctx.interaction.locale
        guild = await _guild_if_owner(ctx.interaction)
        if guild is None:
            refusal = _nothing_deleted(catalog, locale, "kit-privacy-server.owner-only")
            await ctx.respond(**ui.render(refusal), ephemeral=True)
            return
        stored = await privacy.count_guild(guild.id)
        what = catalog.text(locale, "kit-privacy-server", bot=bot.name)
        layout = ui.Container(
            ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-server.title')}"),
            ui.TextDisplay(f"{what}\n{_listed(catalog, locale, stored)}"),
            ui.TextDisplay(catalog.text(locale, "kit-privacy-server.warning")),
            ui.Row(
                ui.Button(
                    self.open_delete_server_data_modal,
                    label=catalog.text(locale, "kit-privacy-server.confirm"),
                    style=hikari.ButtonStyle.DANGER,
                ),
                ui.Button(
                    self.back_to_overview,
                    label=catalog.text(locale, "kit-privacy-cancel"),
                    style=hikari.ButtonStyle.SECONDARY,
                ),
            ),
            accent_color=_RED,
        )
        await ctx.edit(layout)

    @risa.handler
    async def open_delete_server_data_modal(self, ctx: risa.ComponentContext, catalog: Catalog = risa.INJECTED) -> None:
        locale = ctx.interaction.locale
        guild = await _guild_if_owner(ctx.interaction)
        if guild is None:
            refusal = _nothing_deleted(catalog, locale, "kit-privacy-server.owner-only")
            await ctx.respond(**ui.render(refusal), ephemeral=True)
            return
        field = hikari.impl.ModalActionRowBuilder().add_text_input(
            _TYPED_NAME,
            catalog.text(locale, "kit-privacy-server.input"),
            placeholder=guild.name,
            max_length=100,
        )
        title = catalog.text(locale, "kit-privacy-server.modal")
        await ctx.interaction.create_modal_response(title, _DELETE_SERVER_MODAL, components=[field])

    @risa.handler
    async def back_to_overview(
        self,
        ctx: risa.ComponentContext,
        privacy: Privacy = risa.INJECTED,
        catalog: Catalog = risa.INJECTED,
        config: Config = risa.INJECTED,
        bot: BotInfo = risa.INJECTED,
    ) -> None:
        overview = _PrivacyOverview(
            locale=str(ctx.interaction.locale),
            stored=await privacy.count_user(ctx.interaction.user.id),
            server_owner=await _guild_if_owner(ctx.interaction) is not None,
            catalog=catalog,
            config=config,
            bot=bot,
        )
        await ctx.edit(overview.render())


@loader.command
class _PrivacyCommand(
    lightbulb.SlashCommand,
    name="kit-cmd-privacy.name",
    description="kit-cmd-privacy.description",
    localize=True,
    contexts=[hikari.ApplicationContextType.GUILD, hikari.ApplicationContextType.BOT_DM],
):
    @lightbulb.invoke
    async def show_privacy(
        self,
        ctx: lightbulb.Context,
        views: risa.Client = lightbulb.di.INJECTED,
        privacy: Privacy = lightbulb.di.INJECTED,
        catalog: Catalog = lightbulb.di.INJECTED,
        config: Config = lightbulb.di.INJECTED,
        bot: BotInfo = lightbulb.di.INJECTED,
    ) -> None:
        overview = _PrivacyOverview(
            locale=str(ctx.interaction.locale),
            stored=await privacy.count_user(ctx.interaction.user.id),
            server_owner=await _guild_if_owner(ctx.interaction) is not None,
            catalog=catalog,
            config=config,
            bot=bot,
        )
        await ctx.respond(**await views.build(overview), ephemeral=True)


@loader.listener(hikari.ModalInteractionCreateEvent)
async def _delete_after_the_typed_name(
    event: hikari.ModalInteractionCreateEvent,
    privacy: Privacy = lightbulb.di.INJECTED,
    catalog: Catalog = lightbulb.di.INJECTED,
    bot: BotInfo = lightbulb.di.INJECTED,
) -> None:
    interaction = event.interaction
    if interaction.custom_id not in {_DELETE_USER_MODAL, _DELETE_SERVER_MODAL}:
        return
    locale = interaction.locale
    typed = next(
        (field.value for row in interaction.components for field in row.components if field.custom_id == _TYPED_NAME),
        "",
    )
    typed = typed.strip().casefold()

    guild: hikari.Guild | None = None
    refusal: str | None = None
    if interaction.custom_id == _DELETE_USER_MODAL:
        if typed != interaction.user.username.casefold():
            refusal = "kit-privacy-delete.mismatch"
    elif (guild := await _guild_if_owner(interaction)) is None:
        refusal = "kit-privacy-server.owner-only"
    elif typed != guild.name.casefold():
        refusal = "kit-privacy-server.mismatch"
    if refusal is not None:
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            **ui.render(_nothing_deleted(catalog, locale, refusal)),
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    # Discord drops an answer that comes after three seconds, and the hub may take longer.
    await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_UPDATE)
    try:
        if guild is None:
            await privacy.delete_user(interaction.user.id)
            finished = ui.Container(
                ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-deleting.title')}"),
                ui.TextDisplay(catalog.text(locale, "kit-privacy-deleting")),
                accent_color=bot.accent_color,
            )
        else:
            deleted = await privacy.delete_guild(guild.id)
            if deleted:
                what = f"{catalog.text(locale, 'kit-privacy-server-deleted', bot=bot.name)}\n"
                what += _listed(catalog, locale, deleted)
            else:
                what = catalog.text(locale, "kit-privacy-server-deleted.nothing", bot=bot.name)
            finished = ui.Container(
                ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-server-deleted.title')}"),
                ui.TextDisplay(what),
                accent_color=bot.accent_color,
            )
    except HubError as exc:
        # A call that ran out of time may still have been carried out, so the answer must not say nothing happened.
        _LOGGER.warning("Could not confirm a deletion after a typed confirmation: %s", exc)
        finished = ui.Container(
            ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-unconfirmed.title')}"),
            ui.TextDisplay(catalog.text(locale, "kit-privacy-unconfirmed")),
            accent_color=_RED,
        )
    await interaction.edit_initial_response(**ui.render(finished))


# Resolving risa's client while the extension loads would freeze the registry before the bot is done with it.
@loader.listener(hikari.StartedEvent)
async def _route_clicks_and_resume_deletions(
    _: hikari.StartedEvent,
    client: lightbulb.Client = lightbulb.di.INJECTED,
    views: risa.Client = lightbulb.di.INJECTED,
    privacy: Privacy = lightbulb.di.INJECTED,
) -> None:
    views.add_view(_PrivacyOverview)
    # One task per client: a lightbulb task keeps its state after a cancel, so a shared one breaks the next client.
    # With lightbulb's default of one failure, a single error would stop the daily retry until the next restart.
    client.task(lightbulb.crontrigger("30 4 * * *"), max_failures=-1)(_resume_pending_deletions)
    await _resume_pending_deletions(privacy)


async def _resume_pending_deletions(privacy: Privacy = lightbulb.di.INJECTED) -> None:
    try:
        started = await privacy.resume_deletions()
    except HubError as exc:
        _LOGGER.warning("Could not resume the pending deletions: %s", exc)
        return
    if started:
        _LOGGER.info("Resumed %d pending deletions", started)


async def _guild_if_owner(
    interaction: hikari.CommandInteraction | hikari.ComponentInteraction | hikari.ModalInteraction,
) -> hikari.Guild | None:
    if interaction.guild_id is None:
        return None
    guild = interaction.get_guild() or await interaction.fetch_guild()
    if guild is None or guild.owner_id != interaction.user.id:
        return None
    return guild


def _listed(catalog: Catalog, locale: hikari.Locale | str, stored: Mapping[str, int]) -> str:
    lines: list[str] = []
    for category, count in stored.items():
        key = f"privacy-category-{category}"
        try:
            lines.append(f"- {catalog.text(locale, key, count=count)}")
        except KeyError:
            _LOGGER.warning("The bot's texts have no %s, so /privacy names the category as it is", key)
            lines.append(f"- {category}: {count}")
    return "\n".join(lines)


def _nothing_deleted(catalog: Catalog, locale: hikari.Locale | str, reason: str) -> ui.Container:
    return ui.Container(
        ui.TextDisplay(f"### {catalog.text(locale, 'kit-privacy-not-deleted.title')}"),
        ui.TextDisplay(catalog.text(locale, reason)),
        accent_color=_RED,
    )
