import os
from asyncio import Event, create_subprocess_exec, ensure_future
from asyncio.subprocess import PIPE

import sys


def read_file(path):
	with open(path, 'r', encoding = 'utf-8') as file:
		return file.read()


def write_file(path, content: str):
	temp_path = path + '~'
	dir_path = os.path.dirname(path)

	if not os.path.exists(dir_path):
		os.makedirs(dir_path)

	with open(temp_path, 'w', encoding = 'utf-8') as file:
		file.write(content)
		os.fsync(file.fileno())

	os.rename(temp_path, path)


class CommandResult:
	def __init__(self, code: int, out: bytes, err: bytes):
		self.code = code
		self.out = out
		self.err = err


async def command(*args, use_stdout = False, use_stderr = False, allow_error = False, cwd = None):
	if use_stdout:
		stdout = PIPE
	else:
		stdout = sys.stdout

	if use_stderr:
		stderr = PIPE
	else:
		stderr = sys.stderr

	process = await create_subprocess_exec(*args, stdout = stdout, stderr = stderr, cwd = cwd)
	out, err = await process.communicate()
	res = CommandResult(process.returncode, out, out)

	if not allow_error:
		assert not res.code

	return res


async def command_lines(*args, **kwargs):
	result = command(*args, use_stdout = True, **kwargs)

	return (await result).out.decode().splitlines()


class DirWatcher:
	def __init__(self, dir):
		self._dir = dir
		self._process = None
		self._target_future = None

	async def __aenter__(self):
		async def target():
			while True:
				await self._process.stdout.readline()
				event.set()

		async def watcher():
			await event.wait()
			event.clear()

		event = Event()

		self._process = await create_subprocess_exec('fsevents', '-b', '-l', '0', self._dir, stdout = PIPE)
		self._target_future = ensure_future(target())

		return watcher

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		self._target_future.cancel()
		self._process.kill()
		await self._process.wait()
