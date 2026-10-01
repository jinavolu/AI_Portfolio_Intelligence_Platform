"""Build fails if any broker adapter or agent tool can place, modify or cancel an order."""

import inspect

import pytest

from app.ai.agent import READ_ONLY_TOOL_NAMES
from app.broker import base, fixture_adapter, kite_connect_adapter, kite_mcp_adapter
from app.broker.base import FORBIDDEN_NAME_FRAGMENTS, BrokerAdapter
from app.broker.kite_connect_adapter import READ_ONLY_KITE_METHODS, KiteConnectAdapter
from app.broker.kite_mcp_adapter import READ_TOOLS, KiteMcpAdapter
from app.clock import Clock

ADAPTERS = [BrokerAdapter, fixture_adapter.FixtureBrokerAdapter, KiteConnectAdapter, KiteMcpAdapter]


def _forbidden(name: str) -> bool:
    return any(f in name.lower() for f in FORBIDDEN_NAME_FRAGMENTS)


@pytest.mark.parametrize("cls", ADAPTERS, ids=lambda c: c.__name__)
def test_adapter_has_no_order_methods(cls):
    bad = [n for n, _ in inspect.getmembers(cls) if not n.startswith("__") and _forbidden(n)]
    assert bad == []


def test_all_adapters_in_package_are_checked():
    found = {obj for mod in (base, fixture_adapter, kite_connect_adapter, kite_mcp_adapter)
             for _, obj in inspect.getmembers(mod, inspect.isclass)
             if issubclass(obj, BrokerAdapter) and obj.__module__.startswith("app.broker")}
    assert found <= set(ADAPTERS)


def test_allowlists_contain_no_order_tools():
    assert not [n for n in READ_TOOLS | READ_ONLY_KITE_METHODS if _forbidden(n)]
    assert not [n for n in READ_ONLY_TOOL_NAMES if _forbidden(n)]


def test_mcp_adapter_refuses_order_tools():
    calls = []
    adapter = KiteMcpAdapter(lambda name, args: calls.append(name), Clock())
    for tool in ("place_order", "modify_order", "cancel_order", "place_gtt_order"):
        with pytest.raises(PermissionError):
            adapter._call(tool, {})
    assert calls == []


def test_kite_connect_wrapper_refuses_order_methods():
    class FakeKite:
        def place_order(self, **kw):
            raise AssertionError("must never be reached")

        def holdings(self):
            return []

    adapter = KiteConnectAdapter("k", "t", Clock(), client=FakeKite())
    assert adapter.get_holdings() == []
    with pytest.raises(PermissionError):
        adapter._kite.place_order()
