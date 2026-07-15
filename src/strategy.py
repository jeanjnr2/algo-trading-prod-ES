from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime
from queue import SimpleQueue
from typing import Any, Deque

from bar_builder import TenSecondBarBuilder
from config import RuntimeConfig
from instruments import ResolvedInstruments, resolve_instruments
from bridge import NinjaBridge
from v8_engine import Quote, Signal, TradePrint, V8Engine

LOGGER = logging.getLogger("strategy")

try:
    from nautilus_trader.trading.strategy import Strategy
except Exception:  # Allows static checks without Nautilus installed.
    class Strategy:  # type: ignore[no-redef]
        pass

try:
    from nautilus_trader.model.identifiers import ClientId
except Exception:
    ClientId = None  # type: ignore[assignment]


class StrategyProdV8Nautilus(Strategy):
    """Nautilus wrapper around the pure V8 signal engine.

    This class does not execute orders. It only:
    - consumes Databento trades and quotes,
    - builds the same 10s bars/profile as the backtest,
    - sends LONG/SHORT intentions to NinjaTrader through ZeroMQ,
    - listens to Ninja state to avoid sending signals while a trade is active.
    """

    def __init__(
        self,
        config: RuntimeConfig,
        instrument_id: Any | None = None,
        instruments: ResolvedInstruments | None = None,
    ) -> None:
        super().__init__()
        self.runtime_config = config
        self.instruments = instruments or resolve_instruments(config.instrument)
        self.data_instrument_id = instrument_id or self.instruments.data_instrument_id
        self.engine = V8Engine(config.strategy, config.instrument)
        self.data_client_id = self._client_id(config.databento["client_id"])
        self.bar_builder = TenSecondBarBuilder()
        self.ninja_event_queue: SimpleQueue[dict[str, Any]] = SimpleQueue()
        self.bridge = NinjaBridge(config.ninja, self.ninja_event_queue)
        self.algo_state = "STARTING"
        self.in_position = False
        self.signal_pending = False
        self.ninja_ready = False
        self.current_signal_id = ""
        self.last_quote_bid_ask: tuple[float, float] | None = None
        self.quote_history: Deque[Quote] = deque(maxlen=10000)
        self.last_rollover_check_date: Any | None = None
        self.rollover_blocked = False

    def on_start(self) -> None:
        self._log_state("STARTING", "READY")
        self.bridge.start()
        self._subscribe_market_data()
        LOGGER.info(
            "STRATEGY_STARTED | algo=%s | data_instrument=%s | exec_owner=NINJA | exec_chart_instrument=NINJA_CHART | exec_source=%s | expiry=%s | rollover=%s",
            self.runtime_config.algo_name,
            self.data_instrument_id,
            self.instruments.exec_source,
            self.instruments.expiry_date or "manual",
            self.instruments.rollover_date or "manual",
        )

    def on_stop(self) -> None:
        self.bridge.stop()
        self._log_state(self.algo_state, "STOPPED")
        LOGGER.info("STRATEGY_STOPPED | algo=%s", self.runtime_config.algo_name)

    def on_trade_tick(self, tick: Any) -> None:
        self._process_events()
        trade = self._trade_from_tick(tick)
        if trade is None:
            return

        self._check_rollover_guard(trade.ts)
        for completed in self.bar_builder.flush_until(trade.ts):
            self._on_completed_bar(completed)
        for completed in self.bar_builder.on_trade(trade):
            self._on_completed_bar(completed)

    def on_quote_tick(self, tick: Any) -> None:
        self._process_events()
        quote = self._quote_from_tick(tick)
        if quote is None:
            return
        self._check_rollover_guard(quote.ts)
        if self._ignore_quote_size_only_update(quote):
            return
        self.quote_history.append(quote)
        self.engine.on_quote(quote)
        for completed in self.bar_builder.flush_until(quote.ts):
            self._on_completed_bar(completed)

    def _on_completed_bar(self, completed: Any) -> None:
        self._process_events()
        bar = getattr(completed, "bar", completed)
        for profile_trade in getattr(completed, "profile_trades", []):
            self.engine.on_trade(profile_trade)

        quote = self._quote_at_or_before(bar.ts)
        if quote is None:
            return
        self.engine.on_quote(quote)
        signal = self.engine.on_bar(bar)

        if self.rollover_blocked:
            return
        if not self.ninja_ready:
            return
        if self.in_position or self.signal_pending:
            return
        if not self._in_entry_window(bar.ts):
            return
        if self._force_flat_window(bar.ts):
            return
        if signal is None:
            return

        LOGGER.info(
            "SIGNAL_ACCEPTED | side=%s | comp30=%.3f | exp6=%.3f | body_ticks=%.1f | poc_distance=%.1f | nml_share=%.4f",
            "LONG" if signal.direction == 1 else "SHORT",
            signal.comp30,
            signal.exp_range6,
            signal.body_ticks,
            signal.poc_distance_ticks,
            signal.nml_volume_share,
        )
        self._send_signal_to_ninja(signal)

    def _process_events(self) -> None:
        while not self.ninja_event_queue.empty():
            self._process_ninja_event(self.ninja_event_queue.get())

    def _process_ninja_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "")).upper()
        signal_id = str(event.get("signal_id", ""))
        LOGGER.info("NINJA_EVENT | type=%s | signal_id=%s | payload=%s", event_type, signal_id, event)

        if event_type in {"ACK", "SIGNAL_ACCEPTED"}:
            self.signal_pending = False
            return
        if event_type in {"READY", "HEARTBEAT"}:
            if not self.ninja_ready:
                LOGGER.info("NINJA_LINK_READY | event_type=%s", event_type)
                self._log_ninja_chart_context(event)
                self._log_state(self.algo_state, "READY")
            self.ninja_ready = True
            return
        if event_type == "NINJA_CONNECTION_LOST":
            self.ninja_ready = False
            self._log_state(self.algo_state, "NINJA_CONNECTION_LOST")
            return
        if event_type in {"REJECTED", "SIGNAL_REJECTED"}:
            LOGGER.warning(
                "SIGNAL_REJECTED_BY_NINJA | signal_id=%s | reason=%s",
                signal_id,
                event.get("reason", ""),
            )
            self.signal_pending = False
            self._log_state(self.algo_state, "READY")
            return
        if event_type in {"POSITION_OPEN", "SIGNALS_DISABLED"}:
            self.signal_pending = False
            self.in_position = True
            self.current_signal_id = signal_id or self.current_signal_id
            self._log_state(self.algo_state, "NINJA_POSITION_OPEN")
            return
        if event_type in {"POSITION_FLAT", "SIGNALS_ENABLED"}:
            self._mark_flat(reason=event_type.lower())

    def _check_rollover_guard(self, ts: datetime) -> None:
        local_date = ts.astimezone(self.runtime_config.session.tzinfo).date()
        if self.last_rollover_check_date == local_date:
            return
        self.last_rollover_check_date = local_date

        resolved = resolve_instruments(
            self.runtime_config.instrument,
            as_of=datetime.combine(local_date, datetime.min.time()),
        )
        if resolved.exec_symbol == self.instruments.exec_symbol:
            return

        if not self.rollover_blocked:
            LOGGER.error(
                "ROLLOVER_RESTART_REQUIRED | current_data=%s | new_data=%s | current_front_month=%s | new_front_month=%s | action=pause_new_entries",
                self.instruments.data_symbol,
                resolved.data_symbol,
                self.instruments.exec_symbol,
                resolved.exec_symbol,
            )
        self.rollover_blocked = True
        self._log_state(self.algo_state, "ROLLOVER_BLOCKED")

    def _send_signal_to_ninja(self, signal: Signal) -> None:
        side_text = "LONG" if signal.direction == 1 else "SHORT"
        try:
            ninja_signal = self.bridge.send_signal(self.runtime_config.algo_name, side_text)
        except Exception as exc:
            LOGGER.exception("SIGNAL_SEND_FAILED | side=%s | error=%s", side_text, exc)
            return

        self.signal_pending = True
        self.current_signal_id = ninja_signal.signal_id
        self._log_state(self.algo_state, "SIGNAL_PENDING")

    def _mark_flat(self, reason: str) -> None:
        LOGGER.info("POSITION_FLAT | reason=%s | signal_id=%s", reason, self.current_signal_id)
        self.in_position = False
        self.signal_pending = False
        self.current_signal_id = ""
        self._log_state(self.algo_state, "READY")

    def _log_ninja_chart_context(self, event: dict[str, Any]) -> None:
        chart_instrument = str(event.get("chart_instrument", ""))
        master_instrument = str(event.get("master_instrument", ""))
        account = str(event.get("account", ""))
        expected_hints = self._expected_ninja_contract_hints()
        expected_display = "|".join(expected_hints)

        LOGGER.info(
            "NINJA_CHART_CONTEXT | chart_instrument=%s | master_instrument=%s | account=%s | python_data_contract=%s | expected_ninja_hint=%s",
            chart_instrument or "unknown",
            master_instrument or "unknown",
            account or "unknown",
            self.instruments.data_symbol,
            expected_display or "unknown",
        )

        chart_upper = chart_instrument.upper()
        if expected_hints and chart_instrument and not any(hint in chart_upper for hint in expected_hints):
            LOGGER.warning(
                "NINJA_CONTRACT_CHECK_UNCERTAIN | chart_instrument=%s | python_data_contract=%s | expected_ninja_hint=%s | action=verify_chart_contract",
                chart_instrument,
                self.instruments.data_symbol,
                expected_display,
            )

    def _expected_ninja_contract_hints(self) -> list[str]:
        data_root = self.instruments.data_symbol.split(".", 1)[0].upper()
        match = re.match(r"^([A-Z]+)([FGHJKMNQUVXZ])([0-9])$", data_root)
        if not match:
            return []

        month_by_code = {
            "F": ("01", "JAN"),
            "G": ("02", "FEB"),
            "H": ("03", "MAR"),
            "J": ("04", "APR"),
            "K": ("05", "MAY"),
            "M": ("06", "JUN"),
            "N": ("07", "JUL"),
            "Q": ("08", "AUG"),
            "U": ("09", "SEP"),
            "V": ("10", "OCT"),
            "X": ("11", "NOV"),
            "Z": ("12", "DEC"),
        }
        month = month_by_code.get(match.group(2))
        if month is None:
            return []
        month_number, month_name = month
        root = match.group(1)
        year = "202" + match.group(3)
        return [
            data_root,
            f"{root} {month_number}-{year[-2:]}",
            f"{root} {month_name}{year[-2:]}",
        ]

    def _subscribe_market_data(self) -> None:
        if self.data_instrument_id is None:
            LOGGER.warning("MARKET_DATA_NOT_SUBSCRIBED | reason=instrument_id_not_set")
            return
        try:
            self.subscribe_trade_ticks(self.data_instrument_id, client_id=self.data_client_id)  # type: ignore[attr-defined]
            self.subscribe_quote_ticks(self.data_instrument_id, client_id=self.data_client_id)  # type: ignore[attr-defined]
            LOGGER.info("MARKET_DATA_SUBSCRIBED | instrument=%s", self.data_instrument_id)
        except Exception as exc:
            LOGGER.error("MARKET_DATA_SUBSCRIBE_FAILED | error=%s", exc)

    def _in_entry_window(self, ts: datetime) -> bool:
        local_time = ts.astimezone(self.runtime_config.session.tzinfo).time().isoformat()
        return self.runtime_config.session.start_trading_time <= local_time <= self.runtime_config.session.last_entry_time

    def _force_flat_window(self, ts: datetime) -> bool:
        local_time = ts.astimezone(self.runtime_config.session.tzinfo).time().isoformat()
        return local_time >= self.runtime_config.session.force_flat_time

    def _log_state(self, old: str, new: str) -> None:
        if old == new:
            return
        LOGGER.info("STATE_CHANGED | %s -> %s", old, new)
        self.algo_state = new

    def _ignore_quote_size_only_update(self, quote: Quote) -> bool:
        if not self.runtime_config.databento.get("ignore_quote_tick_size_updates", True):
            return False
        bid_ask = (quote.bid, quote.ask)
        if self.last_quote_bid_ask == bid_ask:
            return True
        self.last_quote_bid_ask = bid_ask
        return False

    def _quote_at_or_before(self, ts: datetime) -> Quote | None:
        for quote in reversed(self.quote_history):
            if quote.ts <= ts:
                return quote
        return None

    @staticmethod
    def _client_id(value: str) -> Any:
        if ClientId is None:
            return value
        return ClientId(value)

    @staticmethod
    def _trade_from_tick(tick: Any) -> TradePrint | None:
        try:
            return TradePrint(
                ts=StrategyProdV8Nautilus._event_ts(tick),
                price=float(getattr(tick, "price")),
                size=float(getattr(tick, "size")),
            )
        except Exception:
            return None

    @staticmethod
    def _quote_from_tick(tick: Any) -> Quote | None:
        try:
            return Quote(
                ts=StrategyProdV8Nautilus._event_ts(tick),
                bid=float(getattr(tick, "bid_price")),
                ask=float(getattr(tick, "ask_price")),
            )
        except Exception:
            try:
                return Quote(
                    ts=StrategyProdV8Nautilus._event_ts(tick),
                    bid=float(getattr(tick, "bid")),
                    ask=float(getattr(tick, "ask")),
                )
            except Exception:
                return None

    @staticmethod
    def _event_ts(event: Any) -> datetime:
        value = getattr(event, "ts_event", None) or getattr(event, "timestamp", None)
        if isinstance(value, datetime):
            return value
        if isinstance(value, int):
            from datetime import timezone

            return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        raise ValueError(f"Unsupported event timestamp: {value!r}")
