import logging
import os
import shlex
import shutil
import threading
from asyncio import Event
from asyncio import Future
from asyncio import create_subprocess_exec
from asyncio import get_running_loop
from asyncio.subprocess import PIPE
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from typing import AsyncIterator
from typing import Awaitable

import fswatch.libfswatch


class UserError(Exception):
    pass


def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def write_file(path: str, content: str) -> None:
    temp_path = path + "~"
    dir_path = os.path.dirname(path)

    if not os.path.exists(dir_path):
        os.makedirs(dir_path)

    with open(temp_path, "w", encoding="utf-8") as file:
        file.write(content)
        os.fsync(file.fileno())

    os.rename(temp_path, path)


class CommandResult:
    def __init__(self, code: int, out: bytes):
        self.code = code
        self.out = out


class CommandError(Exception):
    pass


async def command(
    cmd: list[str], allow_error: bool = False, **kwargs: Any
) -> CommandResult:
    process = await create_subprocess_exec(*cmd, **kwargs)
    out, _ = await process.communicate()
    assert process.returncode is not None
    res = CommandResult(process.returncode, out)

    if not allow_error and res.code:
        raise CommandError(f"Command failed: {shlex.join(cmd)}")

    return res


async def command_lines(cmd: list[str], **kwargs: Any) -> list[str]:
    result = command(cmd, stdout=PIPE, **kwargs)

    return (await result).out.decode().splitlines()


async def join_thread(thread: threading.Thread) -> None:
    loop = get_running_loop()
    future: Future[None] = Future()

    def target() -> None:
        thread.join()
        loop.call_soon_threadsafe(future.set_result, None)

    threading.Thread(target=target, daemon=True).start()

    await future


def remove_directory_really(path: Path) -> None:
    if not path.exists():
        return

    last_exception: Exception

    for _ in range(5):
        try:
            shutil.rmtree(path)
        except OSError as e:
            last_exception = e
            logging.warning(f"warning: While deleting directory at {path}: {e}")
        else:
            break
    else:
        raise last_exception


class _Monitor(fswatch.Monitor):  # type: ignore
    def start(self) -> None:
        fswatch.libfswatch.fsw_start_monitor(self.handle)

    def stop(self) -> None:
        fswatch.libfswatch.fsw_stop_monitor(self.handle)


@asynccontextmanager
async def dir_watcher(dir_path: str) -> AsyncIterator[Callable[[], Awaitable[None]]]:
    loop = get_running_loop()
    event = Event()

    async def watcher_fn() -> None:
        await event.wait()
        event.clear()

    def monitor_callback(
        path: str, evt_time: int, flags: object, flags_num: int, event_num: int
    ) -> None:
        loop.call_soon_threadsafe(event.set)

    monitor = _Monitor()
    monitor.add_path(dir_path)
    monitor.set_recursive()
    monitor.set_callback(monitor_callback)

    thread = threading.Thread(target=monitor.start, daemon=True)
    thread.start()

    try:
        yield watcher_fn
    finally:
        monitor.stop()
        await join_thread(thread)
