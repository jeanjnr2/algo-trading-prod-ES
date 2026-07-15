from __future__ import annotations

import argparse
import logging
import os

from config import load_config
from instruments import resolve_instruments
from logging_config import configure_logging
from strategy import StrategyProdV8Nautilus

LOGGER = logging.getLogger("run_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging("INFO")
    cfg = load_config()

    LOGGER.info("BOOT | algo=%s | static_config=python_object", cfg.algo_name)
    instruments = resolve_instruments(cfg.instrument)
    LOGGER.info(
        "INSTRUMENTS_RESOLVED | data=%s | execution=%s | exec_source=%s | expiry=%s | rollover=%s",
        instruments.data_symbol,
        instruments.exec_symbol,
        instruments.exec_source,
        instruments.expiry_date or "manual",
        instruments.rollover_date or "manual",
    )

    if args.dry_run:
        strategy = StrategyProdV8Nautilus(cfg, instruments=instruments)
        strategy.on_start()
        LOGGER.info("DRY_RUN_READY | press Ctrl+C to stop")
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            strategy.on_stop()
        return

    try:
        from nautilus_trader.config import LoggingConfig, TradingNodeConfig
        from nautilus_trader.live.node import TradingNode
    except Exception as exc:
        LOGGER.error("NAUTILUS_IMPORT_FAILED | error=%s", exc)
        LOGGER.info("TIP | use --dry-run to test logging and Ninja bridge without Nautilus")
        raise

    # Adapter imports are intentionally delayed so the dry-run path works without
    # a full Nautilus adapter environment.
    try:
        from nautilus_trader.adapters.databento import DATABENTO
    except Exception as exc:
        LOGGER.error("ADAPTER_IMPORT_FAILED | error=%s", exc)
        raise

    # Databento adapter names differ across Nautilus versions. Keep this explicit
    # so the first real install can be patched in one place.
    try:
        from nautilus_trader.adapters.databento.config import DatabentoDataClientConfig
        from nautilus_trader.adapters.databento.factories import DatabentoLiveDataClientFactory
    except Exception as exc:
        LOGGER.error("DATABENTO_ADAPTER_IMPORT_FAILED | error=%s", exc)
        raise

    databento_api_key = os.getenv("DATABENTO_API_KEY", "")
    if not databento_api_key:
        raise ValueError("DATABENTO_API_KEY is not set")

    data_client_config = DatabentoDataClientConfig(
        api_key=databento_api_key,
        instrument_provider=None,
        instrument_ids=[instruments.data_instrument_id],
        venue_dataset_map={
            cfg.instrument.databento_venue: cfg.instrument.databento_dataset,
        },
    )

    node_config = TradingNodeConfig(
        trader_id=f"{cfg.algo_name}-TRADER",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={DATABENTO: data_client_config},
        exec_clients={},
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory(DATABENTO, DatabentoLiveDataClientFactory)
    node.build()

    strategy = StrategyProdV8Nautilus(cfg, instruments=instruments)
    node.trader.add_strategy(strategy)
    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
