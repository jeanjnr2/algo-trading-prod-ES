from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str
    databento_dataset: str
    databento_symbol: str
    databento_venue: str
    execution_symbol: str
    execution_venue: str
    execution_instrument_env: str
    rollover_days_before_expiry: int
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
class RuntimeConfig:
    algo_name: str
    instrument: InstrumentConfig
    session: SessionConfig
    strategy: StrategyConfig
    databento: dict
    ninja: dict


class StrategyProdV8Config:
    def build(self) -> RuntimeConfig:
        return RuntimeConfig(
            algo_name="V8_ES",
            instrument=InstrumentConfig(
                symbol="ES",
                databento_dataset="GLBX.MDP3",
                databento_symbol="AUTO_FROM_EXECUTION",
                databento_venue="GLBX",
                execution_symbol="ES",
                execution_venue="CME",
                execution_instrument_env="ES_EXECUTION_INSTRUMENT_ID",
                rollover_days_before_expiry=8,
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
            databento={
                "client_id": "DATABENTO",
                "trade_schema": "trades",
                "quote_schema": "mbp-1",
                "ignore_quote_tick_size_updates": True,
            },
            ninja={
                "signal_pub_bind": "tcp://*:5555",
                "state_pull_bind": "tcp://*:5556",
                "signal_ttl_ms": 2000,
                "heartbeat_interval_sec": 2.0,
                "connection_timeout_sec": 8.0,
            },
        )


def load_config() -> RuntimeConfig:
    return StrategyProdV8Config().build()
