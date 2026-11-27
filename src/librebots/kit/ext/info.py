"""Adds `/info`: the bot's name, description and version, how many servers it serves, who runs it, and links.

Needs `BotInfo`, `Config`, `Hub` and a `Catalog` with kit's texts and the bot's `bot-description` in lightbulb's
dependency injection.
"""

from __future__ import annotations

import logging

import hikari
import lightbulb
from librebots.hub.v1.stats_pb2 import GetStatsRequest
from risa import ui

from librebots.kit.bot import BotInfo
from librebots.kit.config import Config
from librebots.kit.hub import Hub, HubError, call_hub
from librebots.kit.i18n import Catalog

__all__ = ["loader"]

_LOGGER = logging.getLogger(__name__)
# Discord fails a command that gets no answer within three seconds.
_STATS_TIMEOUT = 2

loader = lightbulb.Loader()


@loader.command
class _Info(
    lightbulb.SlashCommand,
    name="kit-cmd-info.name",
    description="kit-cmd-info.description",
    localize=True,
    contexts=[hikari.ApplicationContextType.GUILD, hikari.ApplicationContextType.BOT_DM],
):
    @lightbulb.invoke
    async def show_info(
        self,
        ctx: lightbulb.Context,
        bot: BotInfo = lightbulb.di.INJECTED,
        config: Config = lightbulb.di.INJECTED,
        catalog: Catalog = lightbulb.di.INJECTED,
        hub: Hub = lightbulb.di.INJECTED,
    ) -> None:
        servers: int | None = None
        try:
            stats = await call_hub("GetStats", hub.stats.GetStats(GetStatsRequest(), timeout=_STATS_TIMEOUT))
        except HubError as exc:
            _LOGGER.warning("Showing /info without a server count: %s", exc)
        else:
            servers = next((entry.figures.guild_count for entry in stats.bots if entry.bot == bot.hub_name), None)
            if servers is None:
                _LOGGER.warning("Showing /info without a server count: the hub has no bot named %r", bot.hub_name)

        locale = ctx.interaction.locale
        details = {"version": bot.version, "operator": config.operator_name, "contact": config.operator_contact}
        if servers is None:
            meta = catalog.text(locale, "kit-info-meta-without-servers", **details)
        else:
            meta = catalog.text(locale, "kit-info-meta", servers=servers, **details)
        links = [ui.LinkButton(bot.source_url, label=catalog.text(locale, "kit-info-source"))]
        if config.support_url is not None:
            links.append(ui.LinkButton(config.support_url, label=catalog.text(locale, "kit-info-support")))
        links.append(ui.LinkButton(config.privacy_policy_url, label=catalog.text(locale, "kit-info-privacy-policy")))

        layout = ui.Container(
            ui.TextDisplay(f"### {bot.name}"),
            ui.TextDisplay(catalog.text(locale, "bot-description")),
            ui.TextDisplay(f"-# {meta}\n-# {catalog.text(locale, 'kit-info-privacy')}"),
            ui.Row(*links),
            accent_color=bot.accent_color,
        )
        await ctx.respond(**ui.render(layout), ephemeral=True)
