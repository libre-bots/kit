"""Answers a failed command or click that no handler of the bot took, and logs it.

Needs a `Catalog` with kit's texts (`kit.LOCALES`) and a risa client in lightbulb's dependency injection. Its handlers
run last, so a bot's own error handlers at the default priority come first.

The answer is visible to the caller only, except after a public defer: it then replaces the "thinking" message, and
Discord keeps that message's visibility.
"""

from __future__ import annotations

import logging
import re
import secrets
import traceback
from collections.abc import Sequence

import hikari
import lightbulb
import risa
from risa import ui

from librebots.kit.hub import HubUnavailableError
from librebots.kit.i18n import Catalog

__all__ = ["loader"]

_LOGGER = logging.getLogger(__name__)
_PRIORITY = -100
_RED = 0xE24B4A
# hikari puts the request URL into its HTTP errors, and these URLs carry the interaction token.
_TOKEN_IN_URL = re.compile(r"(/(?:interactions|webhooks)/\d+/)[^/\s?#']+")

loader = lightbulb.Loader()


@loader.error_handler(priority=_PRIORITY)
async def _answer_failed_command(
    exc: lightbulb.exceptions.ExecutionPipelineFailedException, catalog: Catalog = lightbulb.di.INJECTED
) -> bool:
    ctx = exc.context
    layout = _build_error_reply(
        catalog, ctx.interaction.locale, exc.causes, f"Command /{ctx.command_data.qualified_name}"
    )
    try:
        await ctx.respond(**ui.render(layout), ephemeral=True)
    except hikari.HTTPError as error:
        _LOGGER.warning("Could not tell the caller that a command failed: %s", _redact_tokens(str(error)))
    return True


async def _answer_failed_click(
    pipeline: risa.ExecutionPipeline, ctx: risa.ComponentContext, catalog: Catalog = risa.INJECTED
) -> bool:
    layout = _build_error_reply(
        catalog, ctx.interaction.locale, [failure.error for failure in pipeline.failures], "A click"
    )
    try:
        await ctx.respond(**ui.render(layout), ephemeral=True)
    except hikari.HTTPError as error:
        _LOGGER.warning("Could not tell the clicker that a click failed: %s", _redact_tokens(str(error)))
    return True


# Resolving risa's client while the extension loads would freeze the registry before the bot is done with it.
@loader.listener(hikari.StartedEvent)
async def _register_click_fallback(_: hikari.StartedEvent, views: risa.Client = lightbulb.di.INJECTED) -> None:
    views.error_handler(_answer_failed_click, priority=_PRIORITY)


def _build_error_reply(
    catalog: Catalog, locale: hikari.Locale | str, errors: Sequence[Exception], failed: str
) -> ui.Container:
    key, reference = _log_and_pick_text(errors, failed)
    text = catalog.text(locale, key) if reference is None else catalog.text(locale, key, reference=reference)
    return ui.Container(
        ui.TextDisplay(f"### {catalog.text(locale, f'{key}.title')}"),
        ui.TextDisplay(text),
        accent_color=_RED,
    )


def _log_and_pick_text(errors: Sequence[Exception], failed: str) -> tuple[str, str | None]:
    for error in errors:
        if isinstance(error, HubUnavailableError):
            _LOGGER.warning("%s failed because the hub is unavailable: %s", failed, _redact_tokens(str(error)))
            return "kit-error-hub", None
    for error in errors:
        if isinstance(error, hikari.ForbiddenError):
            _LOGGER.info("%s failed for a missing permission: %s", failed, _redact_tokens(str(error)))
            return "kit-error-forbidden", None
    reference = secrets.token_hex(3)
    for error in errors:
        described = _redact_tokens("".join(traceback.format_exception(error)).rstrip())
        _LOGGER.error("%s failed, reference %s\n%s", failed, reference, described)
    return "kit-error-unknown", reference


def _redact_tokens(text: str) -> str:
    return _TOKEN_IN_URL.sub(r"\1***", text)
