import os
from asyncio.subprocess import PIPE

from subprocess import Popen

from git_toxic.util import command, command_lines, read_file, write_file


class Repository:
	def __init__(self, path):
		self.path = path

	def _command_args_prefix(self):
		return 'git', '--git-dir', self.path

	async def _command(self, *args, **kwargs):
		return await command(*self._command_args_prefix(), *args, **kwargs)

	async def _command_lines(self, *args, **kwargs):
		return await command_lines(*self._command_args_prefix(), *args, **kwargs)

	async def rev_list(self, *refs, max_count = None):
		max_count_args = [] if max_count is None else ['--max-count', str(max_count)]

		return await self._command_lines('rev-list', *max_count_args, *refs)

	async def show_ref(self):
		list = await self._command_lines('show-ref')

		def iter_entries():
			for i in list:
				parts = i.split(' ', 1)

				# Ignore entries with line breaks in them.
				if len(parts) == 2:
					k, v = parts

					yield v, k

		return dict(iter_entries())

	async def get_commit_info(self, commit_id):
		lines = await self._command_lines('cat-file', 'commit', commit_id)

		def iter_entries():
			for i in lines:
				if not i:
					return

				yield i.split(' ', 1)

		return dict(iter_entries())

	async def update_ref(self, name, commit_id):
		await self._command('update-ref', name, commit_id)

	async def delete_ref(self, name):
		await self._command('update-ref', '-d', name)

	async def read_config(self, name, default = None):
		result = await self._command_lines('config', name, allow_error = True)

		if result:
			value, = result

			return value
		else:
			return default

	async def export_to_dir(self, commit_id, dir):
		git_process = Popen([*self._command_args_prefix(), 'archive', commit_id], stdout = PIPE)
		tar_process = Popen(['tar', '-x', '-C', dir], stdin = git_process.stdout)

		git_process.wait()
		tar_process.wait()

		assert not git_process.returncode
		assert not tar_process.returncode

	def read_file(self, path):
		return read_file(os.path.join(self.path, path))

	def write_file(self, path, content):
		return write_file(os.path.join(self.path, path), content)

	@classmethod
	async def from_dir(cls, path):
		line, = await command_lines('git', 'rev-parse', '--git-dir', cwd = path)

		return cls(os.path.abspath(line))

	@classmethod
	async def from_cwd(cls):
		return await cls.from_dir('.')
