from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from queue import SimpleQueue
from typing import Any, Deque

from bar_builder import TenSecondBarBuilder
from config import RuntimeConfig
from control import ControlState, ControlWatcher, load_control
from instruments import ResolvedInstruments, resolve_instruments
from v8_engine import Quote, Signal, TradePrint, V8Engine

LOGGER = logging.getLogger("strategy")

try:
    from nautilus_trader.trading.strategy import Strategy
except Exception:  # Allows static checks without Nautilus installed.
    class Strategy:  # type: ignore[no-redef]
        pass

try:
    from nautilus_trader.model.enums import OrderSide, TimeInForce
    from nautilus_trader.model.identifiers import ClientId, ClientOrderId
    from nautilus_trader.model.objects import Price, Quantity
except Exception:
    OrderSide = None  # type: ignore[assignment]
    TimeInForce = None  # type: ignore[assignment]
    ClientId = None  # type: ignore[assignment]
    ClientOrderId = None  # type: ignore[assignment]
    Price = None  # type: ignore[assignment]
    Quantity = None  # type: ignore[assignment]

try:
    from nautilus_trader.adapters.interactive_brokers.common import IBOrderTags
except Exception:
    IBOrderTags = None  # type: ignore[assignment]


class StrategyProdV8Nautilus(Strategy):
    """Nautilus wrapper around the pure V8 engine.

    The pure trading logic lives in `V8Engine`. This wrapper subscribes to
    Databento ticks, applies JSON controls and sends IBKR orders.
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
        if instrument_id is not None:
            self.data_instrument_id = instrument_id
            self.exec_instrument_id = instrument_id
        else:
            self.data_instrument_id = self.instruments.data_instrument_id
            self.exec_instrument_id = self.instruments.exec_instrument_id
        self.engine = V8Engine(config.strategy, config.instrument)
        self.data_client_id = self._client_id(config.databento["client_id"])
        self.exec_client_id = self._client_id(config.ibkr["client_id"])
        self.bar_builder = TenSecondBarBuilder()
        self.control_queue: SimpleQueue[ControlState] = SimpleQueue()
        self.control = load_control(config.control_file)
        self.control_watcher = ControlWatcher(config.control_file, self.control_queue)
        self.algo_state = "STARTING"
        self.in_position = False
        self.entry_order_pending = False
        self.exit_order_pending = False
        self.protected_stop_moved = False
        self.entry_price = 0.0
        self.active_qty = 0
        self.entry_requested_qty = 0
        self.entry_filled_qty = 0
        self.entry_fill_notional = 0.0
        self.position_direction = 0
        self.trade_sequence = 0
        self.last_quote_bid_ask: tuple[float, float] | None = None
        self.quote_history: Deque[Quote] = deque(maxlen=10000)
        self.entry_order_id: Any | None = None
        self.tp_order_id: Any | None = None
        self.sl_order_id: Any | None = None
        self.tp_order: Any | None = None
        self.sl_order: Any | None = None
        self.current_order_ref = ""
        self.last_rollover_check_date: Any | None = None
        self.rollover_blocked = False

    def on_start(self) -> None:
        self._log_state("STARTING", "READY")
        self.control_watcher.start()
        self._subscribe_market_data()
        LOGGER.info(
            "STRATEGY_STARTED | algo=%s | data_instrument=%s | exec_instrument=%s | exec_source=%s | expiry=%s | rollover=%s",
            self.runtime_config.algo_name,
            self.data_instrument_id,
            self.exec_instrument_id,
            self.instruments.exec_source,
            self.instruments.expiry_date or "manual",
            self.instruments.rollover_date or "manual",
        )

    def on_stop(self) -> None:
        self.control_watcher.stop()
        self._log_state(self.algo_state, "STOPPED")
        LOGGER.info("STRATEGY_STOPPED | algo=%s", self.runtime_config.algo_name)

    def on_trade_tick(self, tick: Any) -> None:
        self._process_controls()
        trade = self._trade_from_tick(tick)
        if trade is None:
            return

        self._check_rollover_guard(trade.ts)
        self._force_flat_due(trade.ts)
        # Match the backtest: evaluate completed 10s bars at their close time
        # before this new bucket's first trade enters the POC/NML profile.
        completed_bars = self.bar_builder.flush_until(trade.ts)
        for completed in completed_bars:
            self._on_completed_bar(completed)
        completed_bars = self.bar_builder.on_trade(trade)
        for completed in completed_bars:
            self._on_completed_bar(completed)

    def on_quote_tick(self, tick: Any) -> None:
        self._process_controls()
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
        if self._force_flat_due(quote.ts):
            return
        self._try_move_stop_to_protected(quote)

    def _on_completed_bar(self, completed: Any) -> None:
        self._process_controls()
        bar = getattr(completed, "bar", completed)
        for profile_trade in getattr(completed, "profile_trades", []):
            self.engine.on_trade(profile_trade)
        quote = self._quote_at_or_before(bar.ts)
        if quote is None:
            return
        self.engine.on_quote(quote)
        signal = self.engine.on_bar(bar)
        if self._force_flat_due(bar.ts):
            return
        if not self.control.allow_new_entries:
            return
        if self.in_position or self.entry_order_pending or self.exit_order_pending:
            return
        if not self._in_entry_window(bar.ts):
            return
        if signal is None:
            return
        qty = self._order_quantity()
        if qty <= 0:
            LOGGER.warning("NO_TRADE | reason=quantity_zero")
            return

        LOGGER.info(
            "SIGNAL_ACCEPTED | side=%s | qty=%s | comp30=%.3f | exp6=%.3f | body_ticks=%.1f | poc_distance=%.1f | nml_share=%.4f",
            "LONG" if signal.direction == 1 else "SHORT",
            qty,
            signal.comp30,
            signal.exp_range6,
            signal.body_ticks,
            signal.poc_distance_ticks,
            signal.nml_volume_share,
        )
        self._submit_entry(signal, qty)

    def _process_controls(self) -> None:
        while not self.control_queue.empty():
            previous = self.control
            self.control = self.control_queue.get()
            LOGGER.info(
                "CONTROL_CHANGED | allow_new_entries=%s | contracts=%s | auto_sizing=%s | r_multiple=%.2f | max_contracts=%s | flatten_now=%s | stop_after_flat=%s",
                self.control.allow_new_entries,
                self.control.contracts,
                self.control.enable_auto_sizing,
                self.control.sizing_r_multiple,
                self.control.max_contracts,
                self.control.flatten_now,
                self.control.stop_after_flat,
            )
            if previous.allow_new_entries and not self.control.allow_new_entries:
                self._log_state(self.algo_state, "PAUSED")
            elif not previous.allow_new_entries and self.control.allow_new_entries:
                self._log_state(self.algo_state, "READY")
            if self.control.flatten_now:
                self._flatten_now()

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
                "ROLLOVER_RESTART_REQUIRED | current_execution=%s | new_execution=%s | current_data=%s | new_data=%s | action=pause_new_entries",
                self.instruments.exec_symbol,
                resolved.exec_symbol,
                self.instruments.data_symbol,
                resolved.data_symbol,
            )
        self.rollover_blocked = True
        self.control = ControlState(
            allow_new_entries=False,
            contracts=self.control.contracts,
            enable_auto_sizing=self.control.enable_auto_sizing,
            sizing_r_multiple=self.control.sizing_r_multiple,
            max_contracts=self.control.max_contracts,
            flatten_now=False,
            stop_after_flat=self.control.stop_after_flat,
        )

    def _submit_entry(self, signal: Signal, qty: int) -> None:
        self.trade_sequence += 1
        side_text = "BUY" if signal.direction == 1 else "SELL"
        safe_algo = "".join(ch for ch in self.runtime_config.algo_name if ch.isalnum())[:10]
        order_ref = f"{safe_algo}-{datetime.now(timezone.utc):%y%m%d%H%M%S}-{self.trade_sequence:03d}"
        LOGGER.info("ORDER_SUBMIT_REQUEST | ref=%s | side=%s | qty=%s", order_ref, side_text, qty)

        if OrderSide is None or TimeInForce is None or ClientOrderId is None or Quantity is None:
            LOGGER.error("ORDER_SUBMIT_FAILED | ref=%s | reason=nautilus_order_types_unavailable", order_ref)
            return

        try:
            order_side = OrderSide.BUY if signal.direction == 1 else OrderSide.SELL
            order = self.order_factory.market(  # type: ignore[attr-defined]
                instrument_id=self.exec_instrument_id,
                order_side=order_side,
                quantity=Quantity.from_int(qty),
                time_in_force=TimeInForce.DAY,
                tags=[f"algo={self.runtime_config.algo_name}", f"ref={order_ref}", "leg=entry"],
                client_order_id=ClientOrderId(f"{order_ref}-ENTRY"),
            )
            self.submit_order(order, client_id=self.exec_client_id)  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.exception("ORDER_SUBMIT_FAILED | ref=%s | error=%s", order_ref, exc)
            return

        self.entry_order_pending = True
        self.current_order_ref = order_ref
        self.position_direction = signal.direction
        self.active_qty = qty
        self.entry_requested_qty = qty
        self.entry_filled_qty = 0
        self.entry_fill_notional = 0.0
        self.entry_order_id = getattr(order, "client_order_id", None)
        self._log_state(self.algo_state, "ENTRY_PENDING")

    def on_order_filled(self, event: Any) -> None:
        client_order_id = self._event_client_order_id(event)
        fill_price = self._event_price(event)
        fill_qty = self._event_qty(event)
        LOGGER.info(
            "ORDER_FILLED | order_id=%s | price=%s | qty=%s",
            client_order_id,
            f"{fill_price:.2f}" if fill_price is not None else "unknown",
            fill_qty if fill_qty is not None else "unknown",
        )

        if client_order_id and client_order_id.endswith("-ENTRY"):
            if fill_price is None or fill_qty is None:
                LOGGER.error(
                    "ENTRY_FILL_IGNORED | reason=missing_fill_data | order_id=%s | price=%s | qty=%s",
                    client_order_id,
                    fill_price,
                    fill_qty,
                )
                return
            fill_qty = max(1, int(fill_qty))
            self.entry_filled_qty += fill_qty
            self.entry_fill_notional += fill_price * fill_qty
            self.entry_price = self.entry_fill_notional / self.entry_filled_qty
            self.active_qty = self.entry_filled_qty
            self.in_position = True

            LOGGER.info(
                "ENTRY_FILL_PROGRESS | order_id=%s | filled=%s/%s | avg_price=%.2f",
                client_order_id,
                self.entry_filled_qty,
                self.entry_requested_qty,
                self.entry_price,
            )
            if self.entry_filled_qty < self.entry_requested_qty:
                self._log_state(self.algo_state, "ENTRY_PARTIAL")
                return

            self.entry_order_pending = False
            self.protected_stop_moved = False
            self._log_state(self.algo_state, "IN_POSITION")
            self._submit_bracket_from_entry()
            return

        if client_order_id and (client_order_id.endswith("-TP") or client_order_id.endswith("-SL") or client_order_id.endswith("-FLAT")):
            self._mark_flat(reason=client_order_id.rsplit("-", 1)[-1])

    def on_order_rejected(self, event: Any) -> None:
        self._handle_order_terminal_event("ORDER_REJECTED", event)

    def on_order_denied(self, event: Any) -> None:
        self._handle_order_terminal_event("ORDER_DENIED", event)

    def on_order_canceled(self, event: Any) -> None:
        self._handle_order_terminal_event("ORDER_CANCELED", event)

    def on_position_closed(self, event: Any) -> None:
        realized = getattr(event, "realized_pnl", None)
        LOGGER.info("POSITION_CLOSED | realized_pnl=%s", realized)
        self._mark_flat(reason="position_closed")

    def _submit_bracket_from_entry(self) -> None:
        if self.entry_price <= 0 or self.position_direction == 0:
            return
        tick = self.runtime_config.instrument.tick_size
        tp = self.entry_price + self.position_direction * self.runtime_config.strategy.profit_target_ticks * tick
        sl = self.entry_price - self.position_direction * self.runtime_config.strategy.stop_loss_ticks * tick
        LOGGER.info("BRACKET_SUBMIT_REQUEST | ref=%s | tp=%.2f | sl=%.2f | oca_type=1", self.current_order_ref, tp, sl)

        if OrderSide is None or TimeInForce is None or ClientOrderId is None or Price is None or Quantity is None:
            LOGGER.error("BRACKET_SUBMIT_FAILED | reason=nautilus_order_types_unavailable")
            self._flatten_now()
            return

        exit_side = OrderSide.SELL if self.position_direction == 1 else OrderSide.BUY
        oca_tags = []
        if IBOrderTags is not None:
            oca_tags.append(
                IBOrderTags(
                    ocaGroup=f"{self.current_order_ref}-OCA",
                    ocaType=1,
                    outsideRth=True,
                ).value
            )
        oca_tags.extend([f"algo={self.runtime_config.algo_name}", f"ref={self.current_order_ref}"])

        try:
            self.tp_order = self.order_factory.limit(  # type: ignore[attr-defined]
                instrument_id=self.exec_instrument_id,
                order_side=exit_side,
                quantity=Quantity.from_int(self.active_qty),
                price=self._price(tp),
                time_in_force=TimeInForce.DAY,
                reduce_only=True,
                tags=[*oca_tags, "leg=tp"],
                client_order_id=ClientOrderId(f"{self.current_order_ref}-TP"),
            )
            self.sl_order = self.order_factory.stop_market(  # type: ignore[attr-defined]
                instrument_id=self.exec_instrument_id,
                order_side=exit_side,
                quantity=Quantity.from_int(self.active_qty),
                trigger_price=self._price(sl),
                time_in_force=TimeInForce.DAY,
                reduce_only=True,
                tags=[*oca_tags, "leg=sl"],
                client_order_id=ClientOrderId(f"{self.current_order_ref}-SL"),
            )
            # Submit the protective stop first. IBKR/Nautilus order-list handling
            # can leave one leg inflight; separate OCA submissions are easier to
            # audit and keep the position protected as early as possible.
            self.submit_order(self.sl_order, client_id=self.exec_client_id)  # type: ignore[attr-defined]
            self.submit_order(self.tp_order, client_id=self.exec_client_id)  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.exception("BRACKET_SUBMIT_FAILED | error=%s | action=flatten", exc)
            self._flatten_now()
            return

        self.tp_order_id = getattr(self.tp_order, "client_order_id", None)
        self.sl_order_id = getattr(self.sl_order, "client_order_id", None)
        self.exit_order_pending = False
        LOGGER.info("BRACKET_SUBMITTED | tp_order=%s | sl_order=%s", self.tp_order_id, self.sl_order_id)

    def _try_move_stop_to_protected(self, quote: Quote) -> None:
        if not self.in_position or self.entry_price <= 0 or self.protected_stop_moved or self.sl_order is None:
            return
        tick = self.runtime_config.instrument.tick_size
        trigger = self.entry_price + self.position_direction * self.runtime_config.strategy.protected_stop_trigger_ticks * tick
        market = quote.bid if self.position_direction == 1 else quote.ask
        hit = market >= trigger if self.position_direction == 1 else market <= trigger
        if not hit:
            return
        protected = self.entry_price + self.position_direction * self.runtime_config.strategy.protected_stop_ticks * tick
        LOGGER.info("PROTECTED_STOP_REQUEST | new_stop=%.2f | trigger=%.2f", protected, trigger)
        try:
            self.modify_order(  # type: ignore[attr-defined]
                self.sl_order,
                trigger_price=self._price(protected),
                client_id=self.exec_client_id,
            )
        except Exception as exc:
            LOGGER.exception("PROTECTED_STOP_FAILED | error=%s", exc)
            return
        self.protected_stop_moved = True
        LOGGER.info("PROTECTED_STOP_SUBMITTED | new_stop=%.2f", protected)

    def _flatten_now(self) -> None:
        if self.exit_order_pending:
            LOGGER.info("FLATTEN_SKIPPED | reason=exit_already_pending")
            return
        LOGGER.warning("FLATTEN_REQUESTED | source=control_json")
        try:
            self.cancel_all_orders(self.exec_instrument_id, client_id=self.exec_client_id)  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.warning("CANCEL_ALL_FAILED | error=%s", exc)
        if not self.in_position and not self.entry_order_pending:
            LOGGER.info("FLATTEN_SKIPPED | reason=already_flat_or_no_pending_entry")
            return
        try:
            self.close_all_positions(  # type: ignore[attr-defined]
                self.exec_instrument_id,
                client_id=self.exec_client_id,
                tags=[f"algo={self.runtime_config.algo_name}", f"ref={self.current_order_ref}", "leg=flat"],
            )
        except Exception as exc:
            LOGGER.exception("FLATTEN_FAILED | error=%s", exc)
            return
        self.exit_order_pending = True
        self._log_state(self.algo_state, "FLATTEN_PENDING")

    def _force_flat_due(self, ts: datetime) -> bool:
        local = ts.astimezone(self.runtime_config.session.tzinfo)
        if local.time().isoformat() < self.runtime_config.session.force_flat_time:
            return False
        if self.in_position and not self.exit_order_pending:
            LOGGER.warning("FORCE_FLAT_TIME | action=flatten")
            self._flatten_now()
        return True

    def _in_entry_window(self, ts: datetime) -> bool:
        local_time = ts.astimezone(self.runtime_config.session.tzinfo).time().isoformat()
        return self.runtime_config.session.start_trading_time <= local_time <= self.runtime_config.session.last_entry_time

    def _order_quantity(self) -> int:
        max_contracts = min(self.runtime_config.risk.max_contracts, self.control.max_contracts)
        contracts = self.control.contracts
        if not self.control.enable_auto_sizing:
            return max(self.runtime_config.risk.min_contracts, min(max_contracts, contracts))
        # Account equity is intentionally not guessed here. Wire IBKR account values
        # before enabling real auto-sizing.
        return max(self.runtime_config.risk.min_contracts, min(max_contracts, contracts))

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

    def _handle_order_terminal_event(self, event_name: str, event: Any) -> None:
        client_order_id = self._event_client_order_id(event)
        LOGGER.warning("%s | order_id=%s | event=%s", event_name, client_order_id, event)
        if client_order_id and client_order_id.endswith("-ENTRY"):
            if self.entry_filled_qty > 0:
                LOGGER.warning(
                    "%s_PARTIAL_ENTRY | order_id=%s | filled=%s/%s | action=protect_filled_qty",
                    event_name,
                    client_order_id,
                    self.entry_filled_qty,
                    self.entry_requested_qty,
                )
                self.entry_order_pending = False
                self.in_position = True
                self.entry_price = self.entry_fill_notional / self.entry_filled_qty
                self.active_qty = self.entry_filled_qty
                self.protected_stop_moved = False
                self._log_state(self.algo_state, "IN_POSITION")
                if self.exit_order_pending:
                    LOGGER.warning(
                        "%s_PARTIAL_ENTRY | order_id=%s | bracket_skipped=flatten_pending",
                        event_name,
                        client_order_id,
                    )
                    return
                self._submit_bracket_from_entry()
                return
            self.entry_order_pending = False
            self.position_direction = 0
            self.active_qty = 0
            self._reset_entry_fill_tracking()
            self._log_state(self.algo_state, "READY")
            return

        if client_order_id and (client_order_id.endswith("-TP") or client_order_id.endswith("-SL")):
            if event_name in {"ORDER_REJECTED", "ORDER_DENIED"} and self.in_position:
                LOGGER.error(
                    "%s_EXIT_ORDER | order_id=%s | action=flatten",
                    event_name,
                    client_order_id,
                )
                self._flatten_now()

    def _reset_entry_fill_tracking(self) -> None:
        self.entry_requested_qty = 0
        self.entry_filled_qty = 0
        self.entry_fill_notional = 0.0

    def _mark_flat(self, reason: str) -> None:
        LOGGER.info("POSITION_FLAT | reason=%s | ref=%s", reason, self.current_order_ref)
        self.in_position = False
        self.entry_order_pending = False
        self.exit_order_pending = False
        self.protected_stop_moved = False
        self.entry_price = 0.0
        self.active_qty = 0
        self._reset_entry_fill_tracking()
        self.position_direction = 0
        self.entry_order_id = None
        self.tp_order_id = None
        self.sl_order_id = None
        self.tp_order = None
        self.sl_order = None
        self.current_order_ref = ""
        if self.control.stop_after_flat:
            self.control = ControlState(
                allow_new_entries=False,
                contracts=self.control.contracts,
                enable_auto_sizing=self.control.enable_auto_sizing,
                sizing_r_multiple=self.control.sizing_r_multiple,
                max_contracts=self.control.max_contracts,
                flatten_now=False,
                stop_after_flat=True,
            )
            self._log_state(self.algo_state, "PAUSED")
        else:
            self._log_state(self.algo_state, "READY")

    @staticmethod
    def _client_id(value: str) -> Any:
        if ClientId is None:
            return value
        return ClientId(value)

    @staticmethod
    def _event_client_order_id(event: Any) -> str:
        value = getattr(event, "client_order_id", "") or getattr(event, "order_id", "")
        return str(value)

    @staticmethod
    def _event_price(event: Any) -> float | None:
        for field in ("avg_px", "last_px", "price"):
            value = getattr(event, field, None)
            if value is not None:
                try:
                    return float(value)
                except Exception:
                    continue
        return None

    @staticmethod
    def _event_qty(event: Any) -> int | None:
        for field in ("last_qty", "filled_qty", "quantity", "qty"):
            value = getattr(event, field, None)
            if value is not None:
                try:
                    return int(float(value))
                except Exception:
                    continue
        return None

    @staticmethod
    def _price(value: float) -> Any:
        if Price is None:
            return value
        return Price.from_str(f"{value:.2f}")

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
            return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        raise ValueError(f"Unsupported event timestamp: {value!r}")
