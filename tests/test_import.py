import importlib


def test_shares_the_namespace_with_the_hub_client() -> None:
    importlib.import_module("librebots.kit")
    importlib.import_module("librebots.hub")
