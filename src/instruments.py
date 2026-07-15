from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from config import InstrumentConfig

try:
    from nautilus_trader.model.identifiers import InstrumentId
except Exception:  # Allows dry-run imports without Nautilus.
    InstrumentId = None  # type: ignore[assignment]


ES_QUARTERLY_MONTH_CODES = (
    (3, "H"),
    (6, "M"),
    (9, "U"),
    (12, "Z"),
)


@dataclass(frozen=True)
class ResolvedInstruments:
    data_instrument_id: Any
    exec_instrument_id: Any
    data_symbol: str
    exec_symbol: str
    exec_source: str
    expiry_date: date | None = None
    rollover_date: date | None = None


def resolve_instruments(config: InstrumentConfig, as_of: datetime | None = None) -> ResolvedInstruments:
    """Resolve Databento data instrument from the current ES front month.

    NinjaTrader remains the execution owner. The execution-like symbol here is
    only used to derive the matching concrete Databento futures contract.
    It can be forced with `ES_EXECUTION_INSTRUMENT_ID`, for example ESU6.CME.
    """

    exec_override = os.getenv(config.execution_instrument_env, "").strip()
    expiry_date = None
    rollover_date = None
    if exec_override:
        exec_symbol = exec_override
        exec_source = "env"
    else:
        front = _front_es_instrument(
            symbol=config.execution_symbol,
            exchange=config.execution_venue,
            rollover_days_before_expiry=config.rollover_days_before_expiry,
            as_of=(as_of or datetime.now()).date(),
        )
        exec_symbol = front.instrument_id
        exec_source = "auto_front_month"
        expiry_date = front.expiry_date
        rollover_date = front.rollover_date

    data_root = config.databento_symbol.strip()
    if data_root.upper() == "AUTO_FROM_EXECUTION":
        data_root = exec_symbol.split(".", 1)[0]
    data_symbol = f"{data_root}.{config.databento_venue}"

    return ResolvedInstruments(
        data_instrument_id=_to_instrument_id(data_symbol),
        exec_instrument_id=_to_instrument_id(exec_symbol),
        data_symbol=data_symbol,
        exec_symbol=exec_symbol,
        exec_source=exec_source,
        expiry_date=expiry_date,
        rollover_date=rollover_date,
    )


def _to_instrument_id(value: str) -> Any:
    if InstrumentId is None:
        return value
    return InstrumentId.from_str(value)


@dataclass(frozen=True)
class FrontMonth:
    instrument_id: str
    expiry_date: date
    rollover_date: date


def _front_es_instrument(
    symbol: str,
    exchange: str,
    rollover_days_before_expiry: int,
    as_of: date,
) -> FrontMonth:
    year = as_of.year
    for month, code in ES_QUARTERLY_MONTH_CODES:
        expiry = _third_friday(year, month)
        rollover_date = expiry - timedelta(days=rollover_days_before_expiry)
        if as_of < rollover_date:
            return FrontMonth(
                instrument_id=f"{symbol}{code}{year % 10}.{exchange}",
                expiry_date=expiry,
                rollover_date=rollover_date,
            )

    next_year = year + 1
    expiry = _third_friday(next_year, 3)
    rollover_date = expiry - timedelta(days=rollover_days_before_expiry)
    return FrontMonth(
        instrument_id=f"{symbol}H{next_year % 10}.{exchange}",
        expiry_date=expiry,
        rollover_date=rollover_date,
    )


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    days_until_friday = (4 - first.weekday()) % 7
    first_friday = first + timedelta(days=days_until_friday)
    return first_friday + timedelta(days=14)
