"""Run the web server and periodic snapshot refresh in one container."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from .snapshot import SnapshotError, validate_database

log = logging.getLogger(__name__)


async def supervise(
    *,
    db_path: Path,
    server_command: Sequence[str],
    refresh_command: Sequence[str],
    refresh_interval: timedelta,
    retry_interval: timedelta,
) -> int:
    """Keep the server running and refresh its snapshot when it becomes stale."""
    if refresh_interval <= timedelta(0) or retry_interval <= timedelta(0):
        raise ValueError("refresh and retry intervals must be positive")

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stopping.set)

    children: set[asyncio.subprocess.Process] = set()
    try:
        source_fetched_at, result = await _ensure_snapshot(
            db_path,
            refresh_command,
            stopping,
            children,
        )
        if source_fetched_at is None:
            return result

        server = await _start(server_command, children)
        next_refresh = loop.time() + _seconds_until_due(source_fetched_at, refresh_interval)

        while True:
            event = await _wait_event(
                stopping,
                server=server,
                delay=max(0.0, next_refresh - loop.time()),
            )
            if event == "stop":
                return 0
            if event == "server":
                children.discard(server)
                result = _returncode(server)
                log.error("Server exited with status %d", result)
                return result

            log.info("Snapshot is due; starting refresh")
            refresh = await _start(refresh_command, children)
            event = await _wait_event(stopping, server=server, refresh=refresh)
            children.discard(refresh)
            if event == "stop":
                return 0
            if event == "server":
                children.discard(server)
                result = _returncode(server)
                log.error("Server exited with status %d during refresh", result)
                return result
            result = _returncode(refresh)
            if result == 0 and _snapshot_time(db_path) is not None:
                log.info("Refresh completed; next refresh is due in %s", refresh_interval)
                next_refresh = loop.time() + refresh_interval.total_seconds()
            else:
                log.error("Refresh failed with status %d; retrying in %s", result, retry_interval)
                next_refresh = loop.time() + retry_interval.total_seconds()
    finally:
        await _terminate(children)
        for sig in signals:
            loop.remove_signal_handler(sig)


async def _ensure_snapshot(
    db_path: Path,
    refresh_command: Sequence[str],
    stopping: asyncio.Event,
    children: set[asyncio.subprocess.Process],
) -> tuple[datetime | None, int]:
    source_fetched_at = _snapshot_time(db_path)
    if source_fetched_at is not None:
        return source_fetched_at, 0

    _move_invalid_snapshot(db_path)
    log.info("No valid snapshot found; starting the initial refresh")
    refresh = await _start(refresh_command, children)
    event = await _wait_event(stopping, refresh=refresh)
    children.discard(refresh)
    if event == "stop":
        return None, 0
    result = _returncode(refresh)
    if result != 0:
        log.error("Initial refresh exited with status %d", result)
        return None, result
    source_fetched_at = _snapshot_time(db_path)
    if source_fetched_at is None:
        log.error("Initial refresh exited successfully but produced no valid snapshot")
        return None, 1
    return source_fetched_at, 0


def _snapshot_time(path: Path) -> datetime | None:
    try:
        metadata = validate_database(path)
    except (SnapshotError, duckdb.Error, OSError, ValueError) as exc:
        log.warning("Snapshot is not ready: %s", exc)
        return None
    return metadata.source_fetched_at


def _move_invalid_snapshot(path: Path) -> None:
    if not path.exists():
        return
    invalid = path.with_name(f"{path.stem}.invalid{path.suffix}")
    path.replace(invalid)
    path.with_suffix(f"{path.suffix}.wal").unlink(missing_ok=True)
    log.warning("Moved invalid snapshot to %s", invalid)


def _seconds_until_due(source_fetched_at: datetime, interval: timedelta) -> float:
    if source_fetched_at.tzinfo is None:
        source_fetched_at = source_fetched_at.replace(tzinfo=UTC)
    due_at = source_fetched_at.astimezone(UTC) + interval
    return max(0.0, (due_at - datetime.now(UTC)).total_seconds())


async def _start(
    command: Sequence[str],
    children: set[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    process = await asyncio.create_subprocess_exec(*command)
    children.add(process)
    return process


async def _wait_event(
    stopping: asyncio.Event,
    *,
    server: asyncio.subprocess.Process | None = None,
    refresh: asyncio.subprocess.Process | None = None,
    delay: float | None = None,
) -> str:
    tasks = {"stop": asyncio.create_task(_as_object(stopping.wait()))}
    if server is not None:
        tasks["server"] = asyncio.create_task(_as_object(server.wait()))
    if refresh is not None:
        tasks["refresh"] = asyncio.create_task(_as_object(refresh.wait()))
    if delay is not None:
        tasks["timer"] = asyncio.create_task(_as_object(asyncio.sleep(delay)))
    done, pending = await asyncio.wait(
        tasks.values(),
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return next(name for name in ("stop", "server", "refresh", "timer") if tasks.get(name) in done)


async def _as_object(awaitable: Awaitable[object]) -> object:
    return await awaitable


def _returncode(process: asyncio.subprocess.Process) -> int:
    assert process.returncode is not None
    return process.returncode


async def _terminate(children: set[asyncio.subprocess.Process]) -> None:
    running = [process for process in children if process.returncode is None]
    for process in running:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    if not running:
        return
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(process.wait() for process in running))
    except TimeoutError:
        for process in running:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        await asyncio.gather(*(process.wait() for process in running))
