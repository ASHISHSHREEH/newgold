import asyncio

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.live.config import LiveDataClientConfig, LiveExecClientConfig
from nautilus_trader.live.data_client import LiveDataClient
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.live.factories import LiveDataClientFactory, LiveExecClientFactory
from nautilus_trader.model.identifiers import ClientId

from adapters.mt5.config import MT5DataClientConfig
from adapters.mt5.data import MT5DataClient
from adapters.mt5.exec_config import MT5ExecClientConfig
from adapters.mt5.execution import MT5ExecutionClient
from adapters.mt5.instrument import build_all_instruments


class MT5DataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveDataClient:
        assert isinstance(config, MT5DataClientConfig)
        provider = InstrumentProvider()
        for instrument in build_all_instruments():
            provider.add(instrument)
            cache.add_instrument(instrument)
        return MT5DataClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )


class MT5ExecClientFactory(LiveExecClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: LiveExecClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> LiveExecutionClient:
        assert isinstance(config, MT5ExecClientConfig)
        provider = InstrumentProvider()
        for instrument in build_all_instruments():
            provider.add(instrument)
        return MT5ExecutionClient(
            loop=loop,
            client_id=ClientId(name),
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            config=config,
        )
