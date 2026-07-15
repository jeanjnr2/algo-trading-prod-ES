from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import SimpleQueue
from typing import Any
from uuid import uuid4

LOGGER = logging.getLogger("bridge")


@dataclass(frozen=True)
class NinjaSignal:
    signal_id: str
    algo: str
    side: str
    timestamp_utc: str
    expires_at_utc: str


class NinjaBridge:
    """ZeroMQ bridge between the Python signal engine and NinjaTrader.

    Python publishes only trade intentions. NinjaTrader remains responsible for
    chart instrument, account selection, margin checks, spread checks, entries,
    brackets and position state.
    """

    def __init__(self, config: dict, event_queue: SimpleQueue[dict[str, Any]]) -> None:
        self.config = config
        self.event_queue = event_queue
        self.pub_bind = config["signal_pub_bind"]
        self.state_pull_bind = config["state_pull_bind"]
        self.signal_ttl_ms = int(config["signal_ttl_ms"])
        self.heartbeat_interval_sec = float(config.get("heartbeat_interval_sec", 2.0))
        self.connection_timeout_sec = float(config.get("connection_timeout_sec", 8.0))
        self._context: Any | None = None
        self._pub_socket: Any | None = None
        self._pull_socket: Any | None = None
        self._state_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_ninja_seen_ts = 0.0
        self._ninja_seen = False
        self._ninja_lost_logged = False
        self._heartbeat_paused_for_test = threading.Event()

    def start(self) -> None:
        try:
            import zmq
        except Exception as exc:
            raise RuntimeError("pyzmq is required for NinjaBridge") from exc

        self._context = zmq.Context.instance()
        self._pub_socket = self._context.socket(zmq.PUB)
        self._pub_socket.bind(self.pub_bind)

        self._pull_socket = self._context.socket(zmq.PULL)
        self._pull_socket.setsockopt(zmq.RCVTIMEO, 250)
        self._pull_socket.bind(self.state_pull_bind)

        self._state_thread = threading.Thread(target=self._state_loop, name="ninja-bridge-state", daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="ninja-bridge-heartbeat", daemon=True)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, name="ninja-bridge-monitor", daemon=True)
        self._state_thread.start()
        self._heartbeat_thread.start()
        self._monitor_thread.start()
        LOGGER.info(
            "NINJA_BRIDGE_STARTED | signal_pub=%s | state_pull=%s | signal_ttl_ms=%s | heartbeat_sec=%.1f | timeout_sec=%.1f",
            self.pub_bind,
            self.state_pull_bind,
            self.signal_ttl_ms,
            self.heartbeat_interval_sec,
            self.connection_timeout_sec,
        )

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._state_thread, self._heartbeat_thread, self._monitor_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        if self._pub_socket is not None:
            self._pub_socket.close(linger=0)
        if self._pull_socket is not None:
            self._pull_socket.close(linger=0)
        LOGGER.info("NINJA_BRIDGE_STOPPED")

    def send_signal(self, algo: str, side: str) -> NinjaSignal:
        if self._pub_socket is None:
            raise RuntimeError("NinjaBridge is not started")

        now = datetime.now(timezone.utc)
        expires_at = now.timestamp() + self.signal_ttl_ms / 1000.0
        signal = NinjaSignal(
            signal_id=f"{algo}-{now:%y%m%d%H%M%S}-{uuid4().hex[:8]}",
            algo=algo,
            side=side,
            timestamp_utc=now.isoformat(),
            expires_at_utc=datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        )
        payload = {
            "type": "SIGNAL",
            "signal_id": signal.signal_id,
            "algo": signal.algo,
            "side": signal.side,
            "timestamp_utc": signal.timestamp_utc,
            "expires_at_utc": signal.expires_at_utc,
        }
        self._pub_socket.send_json(payload)
        LOGGER.info(
            "SIGNAL_SENT_TO_NINJA | signal_id=%s | side=%s | expires_at=%s",
            signal.signal_id,
            signal.side,
            signal.expires_at_utc,
        )
        return signal

    def pause_heartbeats_for_test(self) -> None:
        self._heartbeat_paused_for_test.set()

    def resume_heartbeats_for_test(self) -> None:
        self._heartbeat_paused_for_test.clear()

    def _send_bridge_event(self, event_type: str) -> None:
        if self._pub_socket is None:
            return
        payload = {
            "type": event_type,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._pub_socket.send_json(payload)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._heartbeat_paused_for_test.is_set():
                    self._send_bridge_event("HEARTBEAT")
            except Exception as exc:
                LOGGER.warning("NINJA_HEARTBEAT_SEND_FAILED | error=%s", exc)
            self._stop.wait(self.heartbeat_interval_sec)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            if self._ninja_seen:
                elapsed = time.monotonic() - self._last_ninja_seen_ts
                if elapsed > self.connection_timeout_sec and not self._ninja_lost_logged:
                    self._ninja_lost_logged = True
                    LOGGER.error(
                        "NINJA_CONNECTION_LOST | last_seen_sec=%.1f | timeout_sec=%.1f",
                        elapsed,
                        self.connection_timeout_sec,
                    )
                    self.event_queue.put({"type": "NINJA_CONNECTION_LOST", "reason": "heartbeat_timeout"})
            self._stop.wait(1.0)

    def _state_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._pull_socket.recv_string()  # type: ignore[union-attr]
            except Exception:
                continue
            try:
                payload = json.loads(message)
            except Exception:
                LOGGER.warning("NINJA_STATE_IGNORED | reason=invalid_json | raw=%s", message)
                continue
            if not isinstance(payload, dict):
                LOGGER.warning("NINJA_STATE_IGNORED | reason=not_object | raw=%s", message)
                continue
            self._mark_ninja_seen(str(payload.get("type", "")).upper(), payload)
            self.event_queue.put(payload)

    def _mark_ninja_seen(self, event_type: str, payload: dict[str, Any]) -> None:
        was_seen = self._ninja_seen
        was_lost = self._ninja_lost_logged
        self._last_ninja_seen_ts = time.monotonic()
        self._ninja_seen = True
        self._ninja_lost_logged = False

        if event_type == "READY" and not was_seen:
            LOGGER.info("NINJA_READY | payload=%s", payload)
        elif was_lost:
            LOGGER.info("NINJA_RECONNECTED | event_type=%s | payload=%s", event_type, payload)
