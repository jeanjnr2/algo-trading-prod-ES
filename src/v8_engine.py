from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque

from config import InstrumentConfig, StrategyConfig


@dataclass(frozen=True)
class Bar10s:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class TradePrint:
    ts: datetime
    price: float
    size: float


@dataclass(frozen=True)
class Quote:
    ts: datetime
    bid: float
    ask: float


@dataclass(frozen=True)
class Signal:
    direction: int
    entry_quote: Quote
    comp30: float
    exp_range6: float
    body_ticks: float
    er18_aligned: float
    er90_aligned: float
    pre_body_rel6: float
    trade_volume_per_second: float
    poc_price: float
    poc_distance_ticks: float
    nml_volume_share: float


class V8Engine:
    def __init__(self, strategy: StrategyConfig, instrument: InstrumentConfig) -> None:
        self.cfg = strategy
        self.instrument = instrument
        self.bars: Deque[Bar10s] = deque(maxlen=max(1000, strategy.median_lookback_bars + 200))
        self.range6_history: Deque[float] = deque(maxlen=strategy.median_lookback_bars + 20)
        self.range30_history: Deque[float] = deque(maxlen=strategy.median_lookback_bars + 20)
        self.pre_body_mean_history: Deque[float] = deque(maxlen=strategy.median_lookback_bars + 20)
        self.recent_trades: Deque[TradePrint] = deque()
        self.last_quote: Quote | None = None

    @property
    def tick_size(self) -> float:
        return self.instrument.tick_size

    def on_quote(self, quote: Quote) -> None:
        if quote.bid > 0 and quote.ask > 0:
            self.last_quote = quote

    def on_trade(self, trade: TradePrint) -> None:
        if trade.price <= 0 or trade.size <= 0:
            return
        self.recent_trades.append(trade)
        self._prune_trades(trade.ts)

    def on_bar(self, bar: Bar10s) -> Signal | None:
        self.bars.append(bar)
        if len(self.bars) < self._min_bars():
            return None

        current_range6 = self._rolling_range(self.cfg.expansion_window_bars)
        current_range30 = self._rolling_range(self.cfg.compression_window_bars)
        current_pre_body = self._rolling_body_mean(1, self.cfg.pre_body_window_bars)

        self.range6_history.append(current_range6)
        self.range30_history.append(current_range30)
        self.pre_body_mean_history.append(current_pre_body)

        if len(self.range30_history) < self.cfg.median_warmup_bars + 2:
            return None
        if len(self.bars) < self.cfg.er_cap_window_bars + 2:
            return None

        quote = self.last_quote
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            return None
        spread_ticks = (quote.ask - quote.bid) / self.tick_size
        if spread_ticks > self.cfg.max_spread_ticks:
            return None

        direction = 1 if bar.close > bar.open else -1 if bar.close < bar.open else 0
        if direction == 0:
            return None

        comp30 = self._safe_div(current_range30, self._median_history(self.range30_history, 1))
        exp_range6 = self._safe_div(current_range6, self._median_history(self.range6_history, 1))
        pre_body_rel6 = self._safe_div(current_pre_body, self._median_history(self.pre_body_mean_history, 1))
        body_ticks = abs(bar.close - bar.open) / self.tick_size
        er18_aligned = direction * self._efficiency_ratio(self.cfg.er_window_bars)
        er90_aligned = direction * self._efficiency_ratio(self.cfg.er_cap_window_bars)
        trade_volume_per_second = self._rolling_volume_per_second(self.cfg.trade_volume_window_bars)

        if not (
            comp30 <= self.cfg.compression_threshold
            and exp_range6 >= self.cfg.expansion_threshold
            and exp_range6 <= self.cfg.max_expansion_threshold
            and body_ticks >= self.cfg.min_body_ticks
            and er18_aligned >= self.cfg.er_aligned_threshold
            and pre_body_rel6 >= self.cfg.pre_body_rel_threshold
            and er90_aligned <= self.cfg.max_er_cap_aligned
            and trade_volume_per_second >= self.cfg.min_trade_volume_per_second
        ):
            return None

        poc_ok, poc_price, poc_distance_ticks = self._away_from_recent_poc(direction, quote, bar.ts)
        if not poc_ok:
            return None
        nml_ok, nml_share = self._useful_no_mans_land(direction, quote, bar.ts)
        if not nml_ok:
            return None

        return Signal(
            direction=direction,
            entry_quote=quote,
            comp30=comp30,
            exp_range6=exp_range6,
            body_ticks=body_ticks,
            er18_aligned=er18_aligned,
            er90_aligned=er90_aligned,
            pre_body_rel6=pre_body_rel6,
            trade_volume_per_second=trade_volume_per_second,
            poc_price=poc_price,
            poc_distance_ticks=poc_distance_ticks,
            nml_volume_share=nml_share,
        )

    def _min_bars(self) -> int:
        return max(
            self.cfg.compression_window_bars,
            self.cfg.expansion_window_bars,
            self.cfg.pre_body_window_bars + 1,
            self.cfg.er_cap_window_bars,
        ) + 2

    def _rolling_range(self, bars: int) -> float:
        items = list(self.bars)[-bars:]
        return max(x.high for x in items) - min(x.low for x in items)

    def _rolling_body_mean(self, start_bars_ago: int, bars: int) -> float:
        items = list(self.bars)
        selected = items[-start_bars_ago - bars : -start_bars_ago]
        return sum(abs(x.close - x.open) / self.tick_size for x in selected) / bars

    def _median_history(self, values: Deque[float], start_bars_ago: int) -> float:
        items = list(values)
        last_index = len(items) - 1 - start_bars_ago
        if last_index < 0:
            return 0.0
        sample: list[float] = []
        for i in range(last_index, -1, -1):
            value = items[i]
            if value > 0:
                sample.append(value)
            if len(sample) >= self.cfg.median_lookback_bars:
                break
        if len(sample) < self.cfg.median_warmup_bars:
            return 0.0
        return float(statistics.median(sample))

    def _efficiency_ratio(self, bars: int) -> float:
        items = list(self.bars)
        net_move = items[-1].close - items[-1 - bars].close
        path = 0.0
        for i in range(1, bars + 1):
            path += abs(items[-i].close - items[-i - 1].close)
        return 0.0 if path <= 0 else net_move / path

    def _rolling_volume_per_second(self, bars: int) -> float:
        volume = sum(x.volume for x in list(self.bars)[-bars:])
        return volume / (bars * self.cfg.bar_seconds)

    def _prune_trades(self, now: datetime) -> None:
        keep_minutes = max(self.cfg.no_mans_land_lookback_minutes, self.cfg.recent_poc_lookback_minutes)
        cutoff = now - timedelta(minutes=keep_minutes)
        while self.recent_trades and self.recent_trades[0].ts < cutoff:
            self.recent_trades.popleft()

    def _away_from_recent_poc(self, direction: int, quote: Quote, signal_time: datetime) -> tuple[bool, float, float]:
        if not self.recent_trades:
            return False, 0.0, 0.0
        entry_price = quote.ask if direction == 1 else quote.bid
        cutoff = signal_time - timedelta(minutes=self.cfg.recent_poc_lookback_minutes)
        if self.recent_trades[0].ts > cutoff:
            return False, 0.0, 0.0
        volume_by_price: dict[float, float] = {}
        for trade in self.recent_trades:
            if cutoff <= trade.ts <= signal_time:
                price_level = self._round_to_tick(trade.price)
                volume_by_price[price_level] = volume_by_price.get(price_level, 0.0) + trade.size
        if not volume_by_price:
            return False, 0.0, 0.0
        poc_price = max(volume_by_price, key=volume_by_price.get)
        distance_ticks = abs(entry_price - poc_price) / self.tick_size
        is_away = direction * (entry_price - poc_price) > 0
        return is_away and distance_ticks >= self.cfg.min_recent_poc_distance_ticks, poc_price, distance_ticks

    def _useful_no_mans_land(self, direction: int, quote: Quote, signal_time: datetime) -> tuple[bool, float]:
        if not self.recent_trades:
            return False, 0.0
        entry_price = quote.ask if direction == 1 else quote.bid
        target_price = entry_price + direction * self.cfg.profit_target_ticks * self.tick_size
        skip = self.cfg.no_mans_land_skip_entry_ticks * self.tick_size
        if direction == 1:
            useful_low, useful_high = entry_price + skip, target_price
        else:
            useful_low, useful_high = target_price, entry_price - skip
        if useful_low > useful_high:
            useful_low, useful_high = useful_high, useful_low
        cutoff = signal_time - timedelta(minutes=self.cfg.no_mans_land_lookback_minutes)
        total_volume = 0.0
        corridor_volume = 0.0
        for trade in self.recent_trades:
            if cutoff <= trade.ts <= signal_time:
                total_volume += trade.size
                if useful_low - 1e-9 <= trade.price <= useful_high + 1e-9:
                    corridor_volume += trade.size
        if total_volume <= 0:
            return False, 0.0
        share = corridor_volume / total_volume
        return share <= self.cfg.no_mans_land_max_volume_share, share

    def _round_to_tick(self, price: float) -> float:
        return round(price / self.tick_size) * self.tick_size

    @staticmethod
    def _safe_div(numerator: float, denominator: float) -> float:
        return 0.0 if denominator <= 0 else numerator / denominator
