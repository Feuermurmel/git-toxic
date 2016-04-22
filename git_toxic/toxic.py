import os
import random
from functools import partial
from collections import UserDict
from enum import Enum
from json import loads, dumps
from tempfile import TemporaryDirectory
from asyncio import ensure_future, Future, Semaphore, Event

from git_toxic.pytest import read_summary, get_summary_statistics
from git_toxic.util import command, DirWatcher, log, background_task
from git_toxic.git import Repository


_tox_state_file_path = 'toxic/results.json'

_space = chr(0xa0)


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


class ToxResult:
	def __init__(self, success: bool, summary: str):
		self.success = success
		self.summary = summary


class Settings:
	def __init__(self, *, labels_by_state: dict, max_distance: int, command: str, max_tasks: int, resultlog_path: str):
		self.labels_by_state = labels_by_state
		self.max_distance = max_distance
		self.command = command
		self.max_tasks = max_tasks
		self.resultlog_path = resultlog_path


class DefaultDict(UserDict):
	def __init__(self, value_fn):
		super().__init__()

		self._value_fn = value_fn

	def __missing__(self, key):
		value = self._value_fn(key)

		self[key] = value

		return value


class Labelizer:
	# It was actually really hard to find those characters! They had to be rendered as zero-width space in a GUI application, not produce a line-break, be considered different from each other by HFS, not normalize to the empty string and not be considered a white-space character by git.
	_invisible_characters = [chr(0x200b), chr(0x2063)]

	def __init__(self, repository: Repository):
		self._repository = repository

		self._label_refs = set()
		self._label_by_commit_id = { }

	def _find_unused_ref(self, prefix):
		while True:
			name = prefix + ''.join((random.choice(self._invisible_characters) for _ in range(30)))

			if name not in self._label_refs:
				return name

	async def label_commit(self, commit_id, label):
		current_label, current_ref = self._label_by_commit_id.get(commit_id, (None, None))

		if current_label != label:
			if label is None:
				log('Removing label from commit {}.', commit_id[:7])
			else:
				log('Setting label of commit {} to {}.', commit_id[:7], label)

			if current_ref is not None:
				await self._repository.delete_ref(current_ref)
				self._label_refs.remove(current_ref)

			if label is None:
				del self._label_by_commit_id[commit_id]
			else:
				ref = self._find_unused_ref('refs/tags/' + label)

				await self._repository.update_ref(ref, commit_id)
				self._label_refs.add(ref)
				self._label_by_commit_id[commit_id] = label, ref

	async def set_labels(self, labels_by_commit_id):
		for k, v in labels_by_commit_id.items():
			await self.label_commit(k, v)

		for i in set(self._label_by_commit_id) - set(labels_by_commit_id):
			await self.label_commit(i, None)

	async def remove_label_refs(self):
		for i in await self._repository.show_ref():
			if self._is_label(i):
				await self._repository.delete_ref(i)

	async def get_non_label_refs(self):
		refs = await self._repository.show_ref()

		return [v for k, v in refs.items() if not self._is_label(k)]

	@classmethod
	def _is_label(cls, ref):
		return ref[-1] in cls._invisible_characters


class Toxic:
	def __init__(self, repository: Repository, settings: Settings):
		self._repository = repository
		self._settings = settings

		self._labelizer = Labelizer(self._repository)

		self._update_labels_event = Event()
		self._tox_task_semaphore = Semaphore(settings.max_tasks)
		self._result_futures_by_commit_id = { }

		self._commits_by_id = DefaultDict(partial(Commit, self))

	async def _get_reachable_commits(self):
		"""
		Collects all commits reachable from any refs which are not created by this application.

		Returns a dict from commit ID to distance, where distance is the distance to the nearest child to which a ref points.
		"""
		res = { }

		for i in await self._labelizer.get_non_label_refs():
			for j, x in enumerate(await self._repository.rev_list(i)):
				distance = res.get(x)

				if distance is None or distance > j:
					res[x] = j

		return res

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

				if self._settings.resultlog_path is None:
					pytest_summary = None
				else:
					path = os.path.join(temp_dir, self._settings.resultlog_path)

					try:
						pytest_summary = read_summary(path)
					except FileNotFoundError:
						log('Warning: Resultlog file {} not found.', path)

						pytest_summary = None

		self._update_labels_event.set()

		return ToxResult(not result.code, pytest_summary)

	async def _get_label(self, commit_id):
		tree_id = await self._commits_by_id[commit_id].get_tree_id()
		future = self._result_futures_by_commit_id.get(tree_id)

		if future is None:
			future = ensure_future(self._run_tox(tree_id, commit_id))

			self._result_futures_by_commit_id[tree_id] = future

		if future.done():
			result = future.result()
			state = TreeState.success if result.success else TreeState.failure
			label = self._settings.labels_by_state[state]
			summary = result.summary

			if summary is not None and label is not None:
				statistics = get_summary_statistics(summary)

				def iter_parts():
					for c, n in ('e', statistics.errors), ('f', statistics.failures):
						if n:
							yield '{}{}{}'.format(_space, c, n)

				label += ''.join(iter_parts())
		else:
			label = self._settings.labels_by_state[TreeState.pending]

		return label

	async def _check_refs(self):
		log('Reading refs ...')

		distances_by_commit_id = await self._get_reachable_commits()
		commit = [k for k, v in distances_by_commit_id.items() if v < self._settings.max_distance]

		labels_by_commit_id = { }

		for i in sorted(commit, key = distances_by_commit_id.get):
			labels_by_commit_id[i] = await self._get_label(i)

		await self._labelizer.set_labels(labels_by_commit_id)

	def _read_tox_results(self):
		try:
			data = loads(self._repository.read_file(_tox_state_file_path))
		except FileNotFoundError:
			return

		for i in data:
			future = Future()
			future.set_result(ToxResult(i['success'], i['summary']))

			self._result_futures_by_commit_id[i['tree_id']] = future

	def _write_tox_results(self):
		def iter_data():
			for k, v in self._result_futures_by_commit_id.items():
				if v.done():
					result = v.result()

					yield dict(
						tree_id = k,
						success = result.success,
						summary = result.summary)

		data = dumps(list(iter_data()))

		self._repository.write_file(_tox_state_file_path, data)

	async def run(self):
		"""
		Reads existing tags and keeps them updated when refs change.
		"""

		log('Initializing ...')

		await self._labelizer.remove_label_refs()
		self._read_tox_results()

		async def dir_watch_task():
			async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
				while True:
					await watcher()
					self._update_labels_event.set()

		with background_task(dir_watch_task):
			while True:
				self._write_tox_results()
				await self._check_refs()

				await self._update_labels_event.wait()
				self._update_labels_event.clear()

	async def clear_labels(self):
		self._labelizer.remove_label_refs()
