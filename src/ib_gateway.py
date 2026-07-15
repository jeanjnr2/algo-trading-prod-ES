from __future__ import annotations

import os
from logging import Logger


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "")
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def ensure_ib_gateway_running(mode: str, ibkr: dict, logger: Logger) -> None:
    auto_start = _env_bool(
        ibkr.get("auto_start_gateway_env", ""),
        bool(ibkr.get("auto_start_gateway", False)),
    )
    if not auto_start:
        logger.info("IB_GATEWAY_AUTOSTART_DISABLED | mode=%s", mode.upper())
        return

    try:
        from nautilus_trader.adapters.interactive_brokers.config import (
            DockerizedIBGatewayConfig,
        )
        from nautilus_trader.adapters.interactive_brokers.gateway import (
            DockerizedIBGateway,
        )
    except Exception as exc:
        logger.error("IB_GATEWAY_AUTOSTART_IMPORT_FAILED | error=%s", exc)
        raise

    image_env = ibkr.get("gateway_image_env", "")
    image = os.getenv(image_env, "") if image_env else ""
    if not image:
        image = ibkr.get("gateway_image", "ghcr.io/gnzsnz/ib-gateway:stable")

    username = os.getenv("TWS_USERNAME", "")
    password = os.getenv("TWS_PASSWORD", "")
    if not username or not password:
        raise ValueError(
            "TWS_USERNAME and TWS_PASSWORD must be set when IBKR auto gateway is enabled"
        )

    config = DockerizedIBGatewayConfig(
        username=username,
        password=password,
        trading_mode=mode,
        read_only_api=bool(ibkr.get("gateway_read_only_api", False)),
        timeout=int(ibkr.get("gateway_timeout", 300)),
        container_image=image,
    )
    gateway = DockerizedIBGateway(config=config)
    logger.info(
        "IB_GATEWAY_ENSURE_START | mode=%s | container=%s | image=%s",
        mode.upper(),
        gateway.container_name,
        image,
    )
    gateway.start()
    logger.info(
        "IB_GATEWAY_READY | mode=%s | container=%s | host_port=%s",
        mode.upper(),
        gateway.container_name,
        ibkr["paper_port"] if mode == "paper" else ibkr["live_port"],
    )
