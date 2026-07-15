from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from queue import SimpleQueue
from threading import Event, Thread
from time import sleep
from typing import Any

LOGGER = logging.getLogger("control")


@dataclass
class ControlState:
    allow_new_entries: bool = True
    contracts: int = 3
    enable_auto_sizing: bool = False
    sizing_r_multiple: float = 1.25
    max_contracts: int = 3
    flatten_now: bool = False
    stop_after_flat: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ControlState":
        return cls(
            allow_new_entries=bool(data.get("allow_new_entries", True)),
            contracts=int(data.get("contracts", 3)),
            enable_auto_sizing=bool(data.get("enable_auto_sizing", False)),
            sizing_r_multiple=float(data.get("sizing_r_multiple", 1.25)),
            max_contracts=int(data.get("max_contracts", 3)),
            flatten_now=bool(data.get("flatten_now", False)),
            stop_after_flat=bool(data.get("stop_after_flat", False)),
        )


def load_control(path: Path) -> ControlState:
    return ControlState.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))


class ControlWatcher:
    def __init__(self, path: Path, queue: SimpleQueue[ControlState]) -> None:
        self.path = path
        self.queue = queue
        self.stop_event = Event()
        self.thread: Thread | None = None

    def start(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except Exception:
            LOGGER.warning("WATCHDOG_UNAVAILABLE | fallback=polling_1s")
            self.thread = Thread(target=self._poll_loop, daemon=True)
            self.thread.start()
            return

        path = self.path
        queue = self.queue

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):  # type: ignore[no-untyped-def]
                self._reload_if_needed(event)

            def on_created(self, event):  # type: ignore[no-untyped-def]
                self._reload_if_needed(event)

            def on_moved(self, event):  # type: ignore[no-untyped-def]
                self._reload_if_needed(event)

            def _reload_if_needed(self, event):  # type: ignore[no-untyped-def]
                candidates = [getattr(event, "src_path", ""), getattr(event, "dest_path", "")]
                if str(path) not in candidates:
                    return
                try:
                    queue.put(load_control(path))
                except Exception as exc:
                    LOGGER.error("CONTROL_RELOAD_FAILED | error=%s", exc)

        observer = Observer()
        observer.schedule(Handler(), str(path.parent), recursive=False)
        observer.start()

        def run() -> None:
            while not self.stop_event.is_set():
                sleep(0.25)
            observer.stop()
            observer.join(timeout=5)

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()
        LOGGER.info("CONTROL_WATCH_STARTED | file=%s", path)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _poll_loop(self) -> None:
        last_mtime = 0.0
        while not self.stop_event.is_set():
            try:
                mtime = self.path.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    self.queue.put(load_control(self.path))
            except Exception as exc:
                LOGGER.error("CONTROL_POLL_FAILED | error=%s", exc)
            sleep(1.0)
