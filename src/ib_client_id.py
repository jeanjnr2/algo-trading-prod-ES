from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

LOCK_SUFFIX = ".lock"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _instance_id(default: str, ibkr: dict) -> str:
    env_name = ibkr.get("instance_id_env", "")
    return os.getenv(env_name, "") or default


def _load_registry(path: Path) -> dict:
    if not path.exists():
        return {"allocations": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"allocations": {}}


def _save_registry(path: Path, registry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    lock_path = Path(f"{path}{LOCK_SUFFIX}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        import msvcrt

        with lock_path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def allocate_client_id(
    ibkr: dict,
    *,
    mode: str,
    account_id: str,
    algo_name: str,
) -> int:
    env_name = ibkr.get("ibg_client_id_env", "")
    if env_name and os.getenv(env_name, ""):
        return int(os.environ[env_name])

    base = int(ibkr["ibg_client_id"])
    span = int(ibkr.get("ibg_client_id_span", 1))
    path = Path(ibkr.get("client_id_registry", "/app/control/ibkr_client_ids.json"))
    instance_id = _instance_id(algo_name, ibkr)
    key = f"{mode}:{account_id}:{instance_id}"

    with _file_lock(path):
        registry = _load_registry(path)
        allocations = registry.setdefault("allocations", {})
        for raw_client_id, allocation in list(allocations.items()):
            if allocation.get("key") == key:
                allocation["updated_at"] = _now_iso()
                _save_registry(path, registry)
                return int(raw_client_id)

        used = {int(client_id) for client_id in allocations}
        for client_id in range(base, base + span):
            if client_id not in used:
                allocations[str(client_id)] = {
                    "key": key,
                    "algo": algo_name,
                    "mode": mode,
                    "account_id": account_id,
                    "instance_id": instance_id,
                    "pid": os.getpid(),
                    "created_at": _now_iso(),
                    "updated_at": _now_iso(),
                }
                _save_registry(path, registry)
                return client_id

    raise RuntimeError(
        f"No free IBKR client id in range [{base}, {base + span - 1}] for {key}"
    )


def release_client_id(
    ibkr: dict,
    *,
    mode: str,
    account_id: str,
    algo_name: str,
) -> None:
    env_name = ibkr.get("ibg_client_id_env", "")
    if env_name and os.getenv(env_name, ""):
        return

    path = Path(ibkr.get("client_id_registry", "/app/control/ibkr_client_ids.json"))
    instance_id = _instance_id(algo_name, ibkr)
    key = f"{mode}:{account_id}:{instance_id}"
    with _file_lock(path):
        registry = _load_registry(path)
        allocations = registry.setdefault("allocations", {})
        for raw_client_id, allocation in list(allocations.items()):
            if allocation.get("key") == key:
                del allocations[raw_client_id]
        _save_registry(path, registry)
