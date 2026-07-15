from __future__ import annotations

import argparse
import logging
import os

from ib_client_id import allocate_client_id, release_client_id
from ib_gateway import ensure_ib_gateway_running
from config import load_config
from instruments import resolve_instruments
from logging_config import configure_logging
from strategy import StrategyProdV8Nautilus

LOGGER = logging.getLogger("run_live")


def mask_account(account_id: str) -> str:
    if len(account_id) <= 4:
        return "****"
    return f"{account_id[:2]}***{account_id[-4:]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", default="/app/control/v8_es.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def resolve_ibkr_mode_and_account(ibkr: dict) -> tuple[str, str, int]:
    mode = os.getenv(ibkr.get("mode_env", ""), "") or ibkr["trading_mode"]
    mode = mode.strip().lower()
    if mode not in {"paper", "live"}:
        raise ValueError(f"Invalid IBKR trading mode: {mode!r}. Expected 'paper' or 'live'.")

    if mode == "paper":
        account_env = ibkr.get("paper_account_env", "")
        port = ibkr["paper_port"]
    else:
        account_env = ibkr.get("live_account_env", "")
        port = ibkr["live_port"]

    account_id = os.getenv(account_env, "") if account_env else ""
    if not account_id:
        account_id = os.getenv(ibkr["account_env"], "")
    if not account_id:
        raise ValueError(
            f"No IBKR account configured for mode={mode}. "
            f"Set {account_env or ibkr['account_env']} or fallback {ibkr['account_env']}."
        )
    return mode, account_id, port


def main() -> None:
    args = parse_args()
    configure_logging("INFO")
    cfg = load_config(args.control)
    cfg.control_file.parent.mkdir(parents=True, exist_ok=True)

    LOGGER.info("BOOT | algo=%s | static_config=python_object", cfg.algo_name)
    LOGGER.info("CONTROL_FILE | path=%s", cfg.control_file)
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
        LOGGER.info("TIP | use --dry-run to test logging/control without Nautilus")
        raise

    # Adapter imports are intentionally delayed so the dry-run path works without
    # a full Nautilus adapter environment.
    try:
        from nautilus_trader.adapters.databento import DATABENTO
        from nautilus_trader.adapters.interactive_brokers.common import IB
        from nautilus_trader.adapters.interactive_brokers.config import (
            InteractiveBrokersExecClientConfig,
            InteractiveBrokersInstrumentProviderConfig,
        )
        from nautilus_trader.adapters.interactive_brokers.factories import (
            InteractiveBrokersLiveExecClientFactory,
        )
        from nautilus_trader.config import RoutingConfig
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

    ibkr = cfg.ibkr
    databento_api_key = os.getenv("DATABENTO_API_KEY", "")
    if not databento_api_key:
        raise ValueError("DATABENTO_API_KEY is not set")

    trading_mode, account_id, ib_port = resolve_ibkr_mode_and_account(ibkr)
    ib_host = os.getenv(ibkr.get("host_env", ""), "") or ibkr["host"]
    ensure_ib_gateway_running(trading_mode, ibkr, LOGGER)
    ib_client_id = allocate_client_id(
        ibkr,
        mode=trading_mode,
        account_id=account_id,
        algo_name=cfg.algo_name,
    )
    LOGGER.warning(
        "IBKR_MODE_SELECTED | mode=%s | account=%s | host=%s | port=%s | client_id=%s",
        trading_mode.upper(),
        mask_account(account_id),
        ib_host,
        ib_port,
        ib_client_id,
    )

    instrument_provider = InteractiveBrokersInstrumentProviderConfig(
        load_ids=frozenset([instruments.exec_instrument_id]),
    )
    data_client_config = DatabentoDataClientConfig(
        api_key=databento_api_key,
        instrument_provider=None,
        instrument_ids=[instruments.data_instrument_id],
        venue_dataset_map={
            cfg.instrument.databento_venue: cfg.instrument.databento_dataset,
        },
    )
    exec_client_config = InteractiveBrokersExecClientConfig(
        ibg_host=ib_host,
        ibg_port=ib_port,
        ibg_client_id=ib_client_id,
        account_id=account_id,
        instrument_provider=instrument_provider,
        routing=RoutingConfig(default=True),
    )

    node_config = TradingNodeConfig(
        trader_id=f"{cfg.algo_name}-TRADER",
        logging=LoggingConfig(log_level="INFO"),
        data_clients={DATABENTO: data_client_config},
        exec_clients={IB: exec_client_config},
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory(DATABENTO, DatabentoLiveDataClientFactory)
    node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
    node.build()

    strategy = StrategyProdV8Nautilus(cfg, instruments=instruments)
    node.trader.add_strategy(strategy)
    try:
        node.run()
    finally:
        try:
            node.dispose()
        finally:
            release_client_id(
                ibkr,
                mode=trading_mode,
                account_id=account_id,
                algo_name=cfg.algo_name,
            )


if __name__ == "__main__":
    main()
