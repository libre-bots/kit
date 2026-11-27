from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Self

import hikari
import msgspec
from fluent.runtime import FluentBundle
from fluent.runtime.errors import FluentFormatError
from fluent.runtime.resolver import Pattern
from fluent.syntax import FluentParser, ast
from fluent.syntax.visitor import Visitor

__all__ = ["LOCALES", "Catalog", "CatalogError"]

_LOGGER = logging.getLogger(__name__)

LOCALES = Path(__file__).parent / "locales"
"""kit's own texts. A bot loads them before its own: `Catalog.load(LOCALES, "locales")`."""
_ENGLISH = "en-US"


class CatalogError(Exception):
    """The translation files are broken or incomplete."""


class Catalog(msgspec.Struct, frozen=True):
    """Texts from Fluent files, one folder per Discord locale such as `en-US` or `de`."""

    _bundles: Mapping[str, FluentBundle]

    @classmethod
    def load(cls, *folders: Path | str) -> Self:
        """Read the `.ftl` files in the language folders inside each of `folders`.

        English (`en-US`) is required. Raises `CatalogError` for a syntax error, a key defined twice in one language,
        or a text, attribute or variable that a translation has and English does not.
        """
        resources = _read(map(Path, folders))
        if _ENGLISH not in resources:
            raise CatalogError(f"There are no English ({_ENGLISH}) texts")
        keys = {code: _keys(code, parsed) for code, parsed in resources.items()}
        for code, found in keys.items():
            _check_against_english(code, found, keys[_ENGLISH])
        bundles: dict[str, FluentBundle] = {}
        for code, parsed in resources.items():
            bundle = FluentBundle([code], use_isolating=False)
            for resource in parsed:
                bundle.add_resource(resource)
            bundles[code] = bundle
        return cls(_bundles=bundles)

    @property
    def locales(self) -> frozenset[hikari.Locale]:
        """The languages that have texts, for example to offer them in a set-up command."""
        return frozenset(hikari.Locale(code) for code in self._bundles)

    def text(self, locale: hikari.Locale | str, key: str, **args: str | int | float) -> str:
        """Return the text for `key` in `locale`, else in another variant of that language, else in English.

        `key` is a message id, or `message.attribute` for an attribute. Raises `KeyError` if no language has it.
        """
        for code in self._fallbacks(str(locale)):
            bundle = self._bundles[code]
            pattern = _pattern(bundle, key)
            if pattern is not None:
                return _format(bundle, code, key, pattern, args)
        raise KeyError(key)

    def localization_provider(self, key: str) -> Mapping[hikari.Locale, str]:
        """Return `key` in every language that has it; pass this to lightbulb as `localization_provider`."""
        found: dict[hikari.Locale, str] = {}
        for code, bundle in self._bundles.items():
            pattern = _pattern(bundle, key)
            if pattern is not None:
                found[hikari.Locale(code)] = _format(bundle, code, key, pattern, {})
        return found

    def _fallbacks(self, code: str) -> list[str]:
        language = code.partition("-")[0]
        chain = [code] if code in self._bundles else []
        chain += sorted(other for other in self._bundles if other != code and other.partition("-")[0] == language)
        if _ENGLISH not in chain:
            chain.append(_ENGLISH)
        return chain


def _read(folders: Iterable[Path]) -> dict[str, list[ast.Resource]]:
    discord_locales = {locale.value for locale in hikari.Locale}
    resources: dict[str, list[ast.Resource]] = {}
    for folder in folders:
        if not folder.is_dir():
            raise CatalogError(f"{folder} is missing or not a folder")
        for language in sorted(path for path in folder.iterdir() if path.is_dir()):
            if language.name not in discord_locales:
                raise CatalogError(f"{language} is not named after a Discord locale")
            for file in sorted(language.glob("*.ftl")):
                resources.setdefault(language.name, []).append(_parse(file))
    return resources


def _parse(file: Path) -> ast.Resource:
    try:
        source = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CatalogError(f"Cannot read {file}: {exc}") from exc
    resource = FluentParser().parse(source)
    for entry in resource.body:
        if isinstance(entry, ast.Junk):
            line = source.count("\n", 0, entry.span.start if entry.span else 0) + 1
            problem = entry.annotations[0].message if entry.annotations else "unreadable entry"
            raise CatalogError(f"{file}:{line}: {problem}")
    return resource


class _Uses(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.variables: set[str] = set()
        self.messages: set[str] = set()

    def visit_VariableReference(self, node: ast.VariableReference) -> None:
        self.variables.add(node.id.name)
        self.generic_visit(node)

    def visit_MessageReference(self, node: ast.MessageReference) -> None:
        self.messages.add(node.id.name if node.attribute is None else f"{node.id.name}.{node.attribute.name}")
        self.generic_visit(node)


def _uses(pattern: ast.Pattern) -> _Uses:
    visitor = _Uses()
    visitor.visit(pattern)
    return visitor


def _keys(code: str, resources: list[ast.Resource]) -> dict[str, frozenset[str]]:
    uses: dict[str, _Uses] = {}
    defined: set[str] = set()
    for resource in resources:
        for entry in resource.body:
            if not isinstance(entry, ast.Message | ast.Term):
                continue
            name = entry.id.name if isinstance(entry, ast.Message) else f"-{entry.id.name}"
            if name in defined:
                raise CatalogError(f"{code}: {name} is defined twice")
            defined.add(name)
            if isinstance(entry, ast.Term):
                continue
            if entry.value is not None:
                uses[name] = _uses(entry.value)
            for attribute in entry.attributes:
                key = f"{name}.{attribute.id.name}"
                if key in uses:
                    raise CatalogError(f"{code}: {key} is defined twice")
                uses[key] = _uses(attribute.value)
    return _with_referenced_variables(uses)


def _with_referenced_variables(uses: Mapping[str, _Uses]) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for key in uses:
        variables: set[str] = set()
        reached, pending = {key}, [key]
        while pending:
            current = uses.get(pending.pop())
            if current is None:
                continue
            variables |= current.variables
            for reference in current.messages - reached:
                reached.add(reference)
                pending.append(reference)
        result[key] = frozenset(variables)
    return result


def _check_against_english(
    code: str, keys: Mapping[str, frozenset[str]], english: Mapping[str, frozenset[str]]
) -> None:
    for key, variables in keys.items():
        if key not in english:
            raise CatalogError(f"{code}: {key} has no English text")
        if extra := variables - english[key]:
            names = ", ".join(f"${name}" for name in sorted(extra))
            raise CatalogError(f"{code}: {key} uses {names}, which the English text does not")


def _pattern(bundle: FluentBundle, key: str) -> Pattern | None:
    message_id, _, attribute = key.partition(".")
    if not bundle.has_message(message_id):
        return None
    message = bundle.get_message(message_id)
    if attribute:
        return message.attributes.get(attribute)
    return message.value


def _format(bundle: FluentBundle, code: str, key: str, pattern: Pattern, args: Mapping[str, str | int | float]) -> str:
    value, errors = bundle.format_pattern(pattern, dict(args))
    if errors:
        _LOGGER.warning("Formatting %s in %s: %s", key, code, "; ".join(_describe(error) for error in errors))
    return str(value)


def _describe(error: Exception) -> str:
    # Other errors can quote the arguments, and those may hold what users wrote.
    return str(error) if isinstance(error, FluentFormatError) else type(error).__name__
