from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from v8_engine import Bar10s, TradePrint


@dataclass(frozen=True)
class CompletedBar:
    bar: Bar10s
    profile_trades: list[TradePrint]


@dataclass
class _MutableBar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    volume_by_price: dict[float, float]

    def update(self, trade: TradePrint) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += trade.size
        self.volume_by_price[trade.price] = self.volume_by_price.get(trade.price, 0.0) + trade.size

    def freeze(self) -> Bar10s:
        return Bar10s(
            ts=self.start + timedelta(seconds=10),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

    def freeze_completed(self) -> CompletedBar:
        profile_trades = [
            TradePrint(ts=self.start, price=price, size=size)
            for price, size in sorted(self.volume_by_price.items())
        ]
        return CompletedBar(bar=self.freeze(), profile_trades=profile_trades)


class TenSecondBarBuilder:
    def __init__(self) -> None:
        self.current: _MutableBar | None = None

    def on_trade(self, trade: TradePrint) -> list[CompletedBar]:
        start = self._floor_10s(trade.ts)
        output: list[CompletedBar] = []

        if self.current is None:
            self.current = self._new_bar(start, trade)
            return output

        if start == self.current.start:
            self.current.update(trade)
            return output

        if start > self.current.start:
            output.append(self.current.freeze_completed())
            self.current = self._new_bar(start, trade)
            return output

        return output

    def flush_until(self, ts: datetime) -> list[CompletedBar]:
        if self.current is None:
            return []
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if self.current.start + timedelta(seconds=10) > ts:
            return []
        bar = self.current.freeze_completed()
        self.current = None
        return [bar]

    @staticmethod
    def _new_bar(start: datetime, trade: TradePrint) -> _MutableBar:
        return _MutableBar(
            start=start,
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.size,
            volume_by_price={trade.price: trade.size},
        )

    @staticmethod
    def _floor_10s(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        second = ts.second - (ts.second % 10)
        return ts.replace(second=second, microsecond=0)
