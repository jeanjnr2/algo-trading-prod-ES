from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str
    databento_dataset: str
    databento_symbol: str
    databento_venue: str
    ibkr_symbol: str
    ibkr_exchange: str
    ibkr_instrument_env: str
    ibkr_rollover_days_before_expiry: int
    tick_size: float
    point_value: float


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    start_trading_time: str
    last_entry_time: str
    force_flat_time: str

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class StrategyConfig:
    bar_seconds: int
    compression_window_bars: int
    expansion_window_bars: int
    median_lookback_bars: int
    median_warmup_bars: int
    er_window_bars: int
    er_cap_window_bars: int
    trade_volume_window_bars: int
    pre_body_window_bars: int
    no_mans_land_lookback_minutes: int
    no_mans_land_skip_entry_ticks: int
    recent_poc_lookback_minutes: int
    min_recent_poc_distance_ticks: int
    compression_threshold: float
    expansion_threshold: float
    max_expansion_threshold: float
    min_body_ticks: float
    er_aligned_threshold: float
    max_er_cap_aligned: float
    pre_body_rel_threshold: float
    min_trade_volume_per_second: float
    no_mans_land_max_volume_share: float
    max_spread_ticks: int
    profit_target_ticks: int
    stop_loss_ticks: int
    protected_stop_trigger_ticks: int
    protected_stop_ticks: int


@dataclass(frozen=True)
class RiskConfig:
    contracts: int
    enable_auto_sizing: bool
    sizing_r_multiple: float
    min_contracts: int
    max_contracts: int
    es_margin_per_contract: float


@dataclass(frozen=True)
class RuntimeConfig:
    algo_name: str
    instrument: InstrumentConfig
    session: SessionConfig
    strategy: StrategyConfig
    risk: RiskConfig
    databento: dict
    ibkr: dict
    control_file: Path


class StrategyProdV8Config:
    def __init__(self, control_file: str | Path = "/app/control/v8_es.json") -> None:
        self.control_file = Path(control_file)

    def build(self) -> RuntimeConfig:
        return RuntimeConfig(
            algo_name="V8_ES",
            instrument=InstrumentConfig(
                symbol="ES",
                databento_dataset="GLBX.MDP3",
                databento_symbol="AUTO_FROM_EXECUTION",
                databento_venue="GLBX",
                ibkr_symbol="ES",
                ibkr_exchange="CME",
                ibkr_instrument_env="IBKR_ES_INSTRUMENT_ID",
                ibkr_rollover_days_before_expiry=8,
                tick_size=0.25,
                point_value=50.0,
            ),
            session=SessionConfig(
                timezone="America/Chicago",
                start_trading_time="09:30:00",
                last_entry_time="14:15:00",
                force_flat_time="14:55:00",
            ),
            strategy=StrategyConfig(
                bar_seconds=10,
                compression_window_bars=30,
                expansion_window_bars=6,
                median_lookback_bars=360,
                median_warmup_bars=60,
                er_window_bars=18,
                er_cap_window_bars=90,
                trade_volume_window_bars=3,
                pre_body_window_bars=6,
                no_mans_land_lookback_minutes=10,
                no_mans_land_skip_entry_ticks=2,
                recent_poc_lookback_minutes=20,
                min_recent_poc_distance_ticks=10,
                compression_threshold=0.85,
                expansion_threshold=1.5,
                max_expansion_threshold=1.778,
                min_body_ticks=2.0,
                er_aligned_threshold=0.15,
                max_er_cap_aligned=0.157,
                pre_body_rel_threshold=0.776,
                min_trade_volume_per_second=35.0,
                no_mans_land_max_volume_share=0.1052414078104552,
                max_spread_ticks=1,
                profit_target_ticks=15,
                stop_loss_ticks=13,
                protected_stop_trigger_ticks=12,
                protected_stop_ticks=1,
            ),
            risk=RiskConfig(
                contracts=3,
                enable_auto_sizing=False,
                sizing_r_multiple=1.25,
                min_contracts=1,
                max_contracts=100,
                es_margin_per_contract=500.0,
            ),
            databento={
                "client_id": "DATABENTO",
                "trade_schema": "trades",
                "quote_schema": "mbp-1",
                "ignore_quote_tick_size_updates": True,
            },
            ibkr={
                "client_id": "IB",
                "host": "host.docker.internal",
                "host_env": "IBKR_HOST",
                "paper_port": 4002,
                "live_port": 4001,
                "ibg_client_id": 11,
                "ibg_client_id_env": "IBKR_CLIENT_ID",
                "ibg_client_id_span": 20,
                "client_id_registry": "/app/control/ibkr_client_ids.json",
                "instance_id_env": "ALGO_INSTANCE_ID",
                "mode_env": "ALGO_TRADING_MODE",
                "account_env": "TWS_ACCOUNT",
                "paper_account_env": "TWS_ACCOUNT_PAPER",
                "live_account_env": "TWS_ACCOUNT_LIVE",
                "trading_mode": "paper",
                "auto_start_gateway": True,
                "auto_start_gateway_env": "IBKR_AUTO_START_GATEWAY",
                "gateway_read_only_api": False,
                "gateway_timeout": 300,
                "gateway_image_env": "IBKR_GATEWAY_IMAGE",
                "gateway_image": "ghcr.io/gnzsnz/ib-gateway:stable",
            },
            control_file=self.control_file,
        )


def load_config(control_file: str | Path = "/app/control/v8_es.json") -> RuntimeConfig:
    return StrategyProdV8Config(control_file=control_file).build()
