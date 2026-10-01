from app.broker.base import BrokerAdapter, BrokerError
from app.clock import Clock
from app.config import Settings


def build_broker(settings: Settings, clock: Clock) -> BrokerAdapter:
    if settings.broker == "fixture":
        from app.broker.fixture_adapter import FixtureBrokerAdapter

        return FixtureBrokerAdapter(clock)
    if settings.broker == "kite_connect":
        from app.broker.kite_connect_adapter import KiteConnectAdapter

        if not (settings.kite_api_key and settings.kite_access_token):
            raise BrokerError("PI_KITE_API_KEY and PI_KITE_ACCESS_TOKEN must be set")
        return KiteConnectAdapter(settings.kite_api_key.get_secret_value(),
                                  settings.kite_access_token.get_secret_value(), clock)
    if settings.broker == "kite_mcp":
        from app.broker.kite_mcp_adapter import KiteMcpAdapter
        from app.broker.mcp_session import McpSessionBridge

        bridge = McpSessionBridge(settings.kite_mcp_url)
        adapter = KiteMcpAdapter(bridge.call_tool, clock)
        adapter.login_bridge = bridge  # used only by the /api/broker/login endpoint
        return adapter
    raise BrokerError(f"Unknown broker {settings.broker!r}")


__all__ = ["BrokerAdapter", "BrokerError", "build_broker"]
