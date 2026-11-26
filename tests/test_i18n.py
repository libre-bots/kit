import logging
from pathlib import Path

import hikari
import lightbulb
import pytest

from librebots.kit import Catalog, CatalogError

ENGLISH = """
saved = Saved { $count } stars
stars = { $count ->
    [one] One star
   *[other] { $count } stars
}
english-only = Only in English
cmd-star =
    .name = star
    .description = Show the starboard
"""

GERMAN = """
stars = { $count ->
    [one] Ein Stern
   *[other] { $count } Sterne
}
cmd-star =
    .name = stern
    .description = Zeigt das Starboard
"""


def write(root: Path, locale: str, text: str, name: str = "bot.ftl") -> Path:
    folder = root / locale
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / name
    file.write_text(text, encoding="utf-8")
    return file


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    write(tmp_path, "en-US", ENGLISH)
    write(tmp_path, "de", GERMAN)
    return Catalog.load(tmp_path)


def test_formats_a_text_with_arguments(catalog: Catalog) -> None:
    assert catalog.text("en-US", "saved", count=3) == "Saved 3 stars"


@pytest.mark.parametrize(("count", "expected"), [(1, "Ein Stern"), (3, "3 Sterne")])
def test_uses_the_plural_rules_of_the_language(catalog: Catalog, count: int, expected: str) -> None:
    assert catalog.text(hikari.Locale.DE, "stars", count=count) == expected


def test_reads_an_attribute(catalog: Catalog) -> None:
    assert catalog.text("de", "cmd-star.description") == "Zeigt das Starboard"


def test_falls_back_to_english_for_a_text_the_language_lacks(catalog: Catalog) -> None:
    assert catalog.text("de", "english-only") == "Only in English"


def test_falls_back_to_english_for_a_language_that_does_not_ship(catalog: Catalog) -> None:
    assert catalog.text(hikari.Locale.FR, "stars", count=1) == "One star"


def test_falls_back_to_another_variant_of_the_same_language(tmp_path: Path) -> None:
    write(tmp_path, "en-US", "hello = Hello")
    write(tmp_path, "es-ES", "hello = Hola")

    assert Catalog.load(tmp_path).text(hikari.Locale.ES_LATAM, "hello") == "Hola"


def test_raises_key_error_for_an_unknown_text(catalog: Catalog) -> None:
    with pytest.raises(KeyError, match="missing"):
        catalog.text("en-US", "missing")


def test_raises_key_error_for_a_message_without_a_value(catalog: Catalog) -> None:
    with pytest.raises(KeyError, match="cmd-star"):
        catalog.text("en-US", "cmd-star")


def test_returns_the_text_and_warns_when_an_argument_is_missing(
    catalog: Catalog, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="librebots.kit"):
        text = catalog.text("en-US", "saved")

    assert text.startswith("Saved")
    assert "saved" in caplog.text


def test_adds_no_isolation_marks_around_arguments(catalog: Catalog) -> None:
    assert "⁨" not in catalog.text("en-US", "saved", count=3)


def test_lists_the_languages_it_ships(catalog: Catalog) -> None:
    assert catalog.locales == frozenset({hikari.Locale.EN_US, hikari.Locale.DE})


def test_localization_provider_returns_every_language_that_has_the_text(catalog: Catalog) -> None:
    assert catalog.localization_provider("cmd-star.name") == {hikari.Locale.EN_US: "star", hikari.Locale.DE: "stern"}
    assert catalog.localization_provider("english-only") == {hikari.Locale.EN_US: "Only in English"}


class Star(lightbulb.SlashCommand, name="cmd-star.name", description="cmd-star.description", localize=True):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.respond("*")


async def test_localizes_a_lightbulb_command(catalog: Catalog) -> None:
    builder = await Star.as_command_builder(hikari.Locale.EN_US, catalog.localization_provider)

    assert builder.name == "star"
    assert builder.name_localizations == {hikari.Locale.EN_US: "star", hikari.Locale.DE: "stern"}


def test_merges_several_folders(tmp_path: Path) -> None:
    write(tmp_path / "kit", "en-US", "kit-hello = Hello from kit")
    write(tmp_path / "bot", "en-US", "hello = Hello from the bot")

    catalog = Catalog.load(tmp_path / "kit", tmp_path / "bot")

    assert catalog.text("en-US", "kit-hello") == "Hello from kit"
    assert catalog.text("en-US", "hello") == "Hello from the bot"


def test_reads_every_file_of_a_language(tmp_path: Path) -> None:
    write(tmp_path, "en-US", "one = One", name="one.ftl")
    write(tmp_path, "en-US", "two = Two", name="two.ftl")

    assert Catalog.load(tmp_path).text("en-US", "two") == "Two"


def test_ignores_files_next_to_the_language_folders(tmp_path: Path) -> None:
    write(tmp_path, "en-US", "hello = Hello")
    (tmp_path / "README.md").write_text("Translations", encoding="utf-8")

    assert Catalog.load(tmp_path).locales == frozenset({hikari.Locale.EN_US})


def test_rejects_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="missing"):
        Catalog.load(tmp_path / "missing")


def test_rejects_a_folder_that_is_no_discord_locale(tmp_path: Path) -> None:
    write(tmp_path, "en-US", "hello = Hello")
    write(tmp_path, "german", "hello = Hallo")

    with pytest.raises(CatalogError, match="german"):
        Catalog.load(tmp_path)


def test_requires_english(tmp_path: Path) -> None:
    write(tmp_path, "de", "hello = Hallo")

    with pytest.raises(CatalogError, match="en-US"):
        Catalog.load(tmp_path)


def test_rejects_a_syntax_error_with_file_and_line(tmp_path: Path) -> None:
    file = write(tmp_path, "en-US", "hello = Hello\nbroken =\n")

    with pytest.raises(CatalogError, match=rf"{file.name}:2"):
        Catalog.load(tmp_path)


def test_rejects_a_key_defined_twice(tmp_path: Path) -> None:
    write(tmp_path / "kit", "en-US", "hello = Hello")
    write(tmp_path / "bot", "en-US", "hello = Hi")

    with pytest.raises(CatalogError, match="hello"):
        Catalog.load(tmp_path / "kit", tmp_path / "bot")


@pytest.mark.parametrize(
    ("german", "key"),
    [
        ("goodbye = Tschüss", "goodbye"),
        ("cmd-star =\n    .name = stern\n    .usage = /stern", "cmd-star.usage"),
        ("stars = { $amount } Sterne", "$amount"),
    ],
    ids=["text", "attribute", "variable"],
)
def test_rejects_a_translation_english_does_not_have(tmp_path: Path, german: str, key: str) -> None:
    write(tmp_path, "en-US", ENGLISH)
    write(tmp_path, "de", german)

    with pytest.raises(CatalogError, match=rf"de.*{key.replace('$', '[$]')}"):
        Catalog.load(tmp_path)
