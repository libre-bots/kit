# Libre Bots kit

The functions and lightbulb extensions every Libre Bot shares: configuration, the hub connection, translations, UI
helpers, guild reporting and privacy. It is a toolbox, not a framework. A bot calls it from its own start-up code,
and nothing in it runs unless the bot names it.

```sh
pip install librebots-kit
```

## Development

You need [mise](https://mise.jdx.dev).

```sh
mise install
```

- `make lint`: ruff, pyright and `actionlint`
- `make test`: the tests

## Releases

A maintainer runs `release` in the Actions tab on `main` and chooses `patch`, `minor` or `major`, optionally as a
release candidate. The run publishes the package to PyPI.
