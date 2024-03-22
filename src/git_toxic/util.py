import asyncio
import os
import threading
from asyncio import Event
from asyncio import create_subprocess_exec
from asyncio.subprocess import PIPE
from contextlib import asynccontextmanager

import fswatch.libfswatch


class UserError(Exception):
    pass


def read_file(path):
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def write_file(path, content: str):
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


async def command(*args, use_stdout=False, allow_error=False, **kwargs):
    create_subprocess_exec_kwargs = dict()

    if use_stdout:
        create_subprocess_exec_kwargs.update(stdout=PIPE)

    process = await create_subprocess_exec(
        *args, **create_subprocess_exec_kwargs, **kwargs
    )
    out, _ = await process.communicate()
    res = CommandResult(process.returncode, out)

    if not allow_error:
        assert not res.code

    return res


async def command_lines(*args, **kwargs):
    result = command(*args, use_stdout=True, **kwargs)

    return (await result).out.decode().splitlines()


async def join_thread(thread):
    loop = asyncio.get_running_loop()
    future = asyncio.Future()

    def target():
        thread.join()
        loop.call_soon_threadsafe(future.set_result, None)

    threading.Thread(target=target, daemon=True).start()

    await future


class _Monitor(fswatch.Monitor):
    def start(self):
        fswatch.libfswatch.fsw_start_monitor(self.handle)

    def stop(self):
        fswatch.libfswatch.fsw_stop_monitor(self.handle)


@asynccontextmanager
async def dir_watcher(dir_path):
    loop = asyncio.get_running_loop()
    event = Event()

    async def watcher_fn():
        await event.wait()
        event.clear()

    def monitor_callback(path, evt_time, flags, flags_num, event_num):
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
