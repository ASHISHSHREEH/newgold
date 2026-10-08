from nautilus_trader.live.config import LiveDataClientConfig


class MT5DataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for MT5DataClient.

    Parameters
    ----------
    poll_interval_secs : float
        How often the worker thread polls MT5 for new ticks/bars (seconds).
    price_precisions : dict[str, int]
        Per-symbol price decimal precision, e.g. {"GOLD": 2, "NAS100": 1}.
    size_precisions : dict[str, int]
        Per-symbol lot-size decimal precision, e.g. {"GOLD": 2}.
    symbols : tuple[str, ...]
        MT5 symbol names to subscribe to (must match broker's exact names).
    terminal_path : str or None
        Absolute path to terminal64.exe.  None = auto-detect.
    login : int or None
        MT5 account number.  None = use the already-logged-in account.
    password : str or None
        MT5 account password.
    server : str or None
        MT5 broker server name, e.g. "FxPro-MT5 Demo".
    """

    poll_interval_secs: float = 0.25
    price_precisions: dict[str, int] = {}
    size_precisions: dict[str, int] = {}
    symbols: tuple[str, ...] = ("GOLD",)
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None
