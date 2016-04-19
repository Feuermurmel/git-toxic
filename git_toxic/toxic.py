import os
import random
from functools import partial
from collections import UserDict
from enum import Enum
from json import loads, dumps
from tempfile import TemporaryDirectory
from asyncio import ensure_future, Future, Semaphore, Event

from git_toxic.util import command, DirWatcher, log, background_task
from git_toxic.git import Repository


# It was actually really hard to find those characters! They had to be rendered as zero-width space in a GUI application, not produce a line-break, be considered different from each other by HFS, not normalize to the empty string and not considered a white-space character by git.
_invisible_characters = [chr(0x200b), chr(0x2063)]

_tox_state_file_path = 'toxic/results.json'


def _is_label(ref):
	return ref[-1] in _invisible_characters


class Commit:
	def __init__(self, toxic: 'Toxic', commit_id):
		self._toxic = toxic
		self._commit_id = commit_id
		self._tree_id_future = None

	async def get_tree_id(self):
		if self._tree_id_future is None:
			async def get():
				info = await self._toxic._repository.get_commit_info(self._commit_id)

				return info['tree']

			self._tree_id_future = ensure_future(get())

		return await self._tree_id_future


class TreeState(Enum):
	pending = 'pending'
	success = 'success'
	failure = 'failure'


class Settings:
	def __init__(self, *, labels_by_state: dict, max_distance: int, command: str, max_tasks: int):
		self.labels_by_state = labels_by_state
		self.max_distance = max_distance
		self.command = command
		self.max_tasks = max_tasks


class DefaultDict(UserDict):
	def __init__(self, value_fn):
		super().__init__()

		self._value_fn = value_fn

	def __missing__(self, key):
		value = self._value_fn(key)

		self[key] = value

		return value


class Toxic:
	def __init__(self, repository: Repository, settings: Settings):
		self._repository = repository
		self._settings = settings

		self._update_labels_event = Event()
		self._tox_task_semaphore = Semaphore(settings.max_tasks)
		self._result_futures_by_commit_id = { }

		self._commits_by_id = DefaultDict(partial(Commit, self))
		self._label_refs = set()
		self._state_by_commit_id = { }

	def _find_unused_ref(self, prefix):
		while True:
			name = prefix + ''.join((random.choice(_invisible_characters) for _ in range(30)))

			if name not in self._label_refs:
				return name

	async def _get_refs(self):
		refs = await self._repository.show_ref()

		return [v for k, v in refs.items() if not _is_label(k)]

	async def _get_reachable_commits(self):
		"""
		Collects all commits reachable from any refs which are not created by this application.

		Returns a dict from commit ID to distance, where distance is the distance to the nearest child to which a ref points.
		"""
		res = { }

		for i in await self._get_refs():
			for j, x in enumerate(await self._repository.rev_list(i)):
				distance = res.get(x)

				if distance is None or distance > j:
					res[x] = j

		return res

	async def _set_label(self, commit_id, state):
		if state is None:
			label = None
		else:
			label = self._settings.labels_by_state[state]

		current_label, current_ref = self._state_by_commit_id.get(commit_id, (None, None))

		if current_label != label:
			if label is None:
				log('Removing label from commit {}.', commit_id[:7])
			else:
				log('Setting label of commit {} to {}.', commit_id[:7], label)

			if current_ref is not None:
				await self._repository.delete_ref(current_ref)
				self._label_refs.remove(current_ref)

			if label is None:
				del self._state_by_commit_id[commit_id]
			else:
				ref = self._find_unused_ref('refs/tags/' + label)

				await self._repository.update_ref(ref, commit_id)
				self._label_refs.add(ref)
				self._state_by_commit_id[commit_id] = label, ref

	async def _run_tox(self, tree_id, commit_id_hint):
		async with self._tox_task_semaphore:
			log('Running tox for commit {} ...'.format(commit_id_hint[:7]))

			with TemporaryDirectory() as temp_dir:
				await self._repository.export_to_dir(tree_id, temp_dir)

				result = await command(
					'bash',
					'-c',
					self._settings.command,
					cwd = temp_dir,
					allow_error = True)

		self._update_labels_event.set()

		return [TreeState.success, TreeState.failure][bool(result.code)]

	async def _check_refs(self):
		log('Reading refs ...')

		distances_by_commit_id = await self._get_reachable_commits()
		commits_to_label = sorted((k for k, v in distances_by_commit_id.items() if v < self._settings.max_distance), key = distances_by_commit_id.get)

		for i in commits_to_label:
			tree_id = await self._commits_by_id[i].get_tree_id()

			future = self._result_futures_by_commit_id.get(tree_id)

			if future is None:
				future = ensure_future(self._run_tox(tree_id, i))

				self._result_futures_by_commit_id[tree_id] = future
			elif future.done():
				await self._set_label(i, future.result())

		for i in set(self._state_by_commit_id) - set(distances_by_commit_id):
			await self._set_label(i, None)

	def _read_tox_results(self):
		try:
			data = loads(self._repository.read_file(_tox_state_file_path))
		except FileNotFoundError:
			return

		for i in data:
			future = Future()
			future.set_result(TreeState(i['state']))

			self._result_futures_by_commit_id[i['tree_id']] = future

	def _write_tox_results(self):
		def iter_data():
			for k, v in self._result_futures_by_commit_id.items():
				if v.done():
					yield dict(tree_id = k, state = v.result().value)

		data = dumps(list(iter_data()))

		self._repository.write_file(_tox_state_file_path, data)

	async def run(self):
		"""
		Reads existing tags and keeps them updated when refs change.
		"""

		log('Initializing ...')

		await self.clear_labels()
		self._read_tox_results()

		async def dir_watch_task():
			async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
				while True:
					await watcher()
					self._update_labels_event.set()

		with background_task(dir_watch_task):
			while True:
				await self._check_refs()
				self._write_tox_results()

				await self._update_labels_event.wait()
				self._update_labels_event.clear()

	async def clear_labels(self):
		for i in await self._repository.show_ref():
			if _is_label(i):
				await self._repository.delete_ref(i)
