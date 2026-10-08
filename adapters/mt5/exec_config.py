from nautilus_trader.live.config import LiveExecClientConfig, RoutingConfig


class MT5ExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for MT5ExecutionClient.

    Parameters
    ----------
    poll_interval_secs : float
        How often the worker thread polls MT5 for account updates.
    price_precisions : dict[str, int]
        Per-symbol price decimal precision, e.g. {"GOLD": 2, "NAS100": 1}.
    size_precisions : dict[str, int]
        Per-symbol lot-size decimal precision, e.g. {"GOLD": 2}.
    symbols : tuple[str, ...]
        MT5 symbol names managed by this client.
    """

    poll_interval_secs: float = 1.0
    price_precisions: dict[str, int] = {}
    size_precisions: dict[str, int] = {}
    symbols: tuple[str, ...] = ("GOLD",)
    routing: RoutingConfig = RoutingConfig(venues=frozenset({"FXPRO"}))
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None
