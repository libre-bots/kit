from __future__ import annotations

import msgspec

__all__ = ["BotInfo"]


class BotInfo(msgspec.Struct, frozen=True, kw_only=True):
    """What `/info` shows about the bot. Its one-line description is the text `bot-description` in the bot's files.

    `hub_name` is the bot's name in the hub's `HUB_BOT_TOKENS`, such as `"starboard"`.
    """

    name: str
    hub_name: str
    version: str
    accent_color: int
    source_url: str
