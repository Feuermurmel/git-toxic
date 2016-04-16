import os
import random
from asyncio import ensure_future, Future
from collections import UserDict
from enum import Enum
from json import loads, dumps
from tempfile import TemporaryDirectory

from git_toxic.util import command, DirWatcher, log
from git_toxic.git import Repository


# It was actually really hard to find those characters! They had to be rendered as zero-width space in a GUI application, not produce a line-break, be considered different from each other by HFS, not normalize to the empty string and not considered a white-space character by git.
_invisible_characters = [chr(0x200b), chr(0x2063)]

_tox_state_file_path = 'toxic/results.json'


def _is_label(ref):
	return ref[-1] in _invisible_characters


class Commit:
	def __init__(self, repository: Repository, commit_id):
		self.repository = repository
		self.commit_id = commit_id
		self._tree_id_future = None

	async def get_tree_id(self):
		if self._tree_id_future is None:
			async def get():
				info = await self.repository.get_commit_info(self.commit_id)

				return info['tree']

			self._tree_id_future = ensure_future(get())

		return await self._tree_id_future


class TreeState(Enum):
	pending = 'pending'
	success = 'success'
	failure = 'failure'


class Tree:
	def __init__(self, repository: Repository, tree_id):
		self.repository = repository
		self.tree_id = tree_id
		self._tox_result_future = None

	async def _run_tox(self, commit_id_hint):
		with TemporaryDirectory() as temp_dir:
			log('Running tox for commit {} ...'.format(commit_id_hint[:7]))

			await self.repository.export_to_dir(self.tree_id, temp_dir)

			result = await command(
				'tox',
				cwd = temp_dir,
				allow_error = True)

			return [TreeState.success, TreeState.failure][bool(result.code)]

	def set_tox_result(self, result):
		self._tox_result_future = Future()
		self._tox_result_future.set_result(result)

	def get_result_if_available(self):
		future = self._tox_result_future

		if future is not None and future.done():
			return future.result()
		else:
			return TreeState.pending

	async def get_result(self, commit_id_hint):
		if self._tox_result_future is None:
			self._tox_result_future = ensure_future(self._run_tox(commit_id_hint))

		return await self._tox_result_future


class DefaultDict(UserDict):
	def __init__(self, value_fn):
		super().__init__()

		self._value_fn = value_fn

	def __missing__(self, key):
		value = self._value_fn(key)

		self[key] = value

		return value


class Toxic:
	def __init__(self, repository: Repository, labels_by_state: dict, max_distance):
		self._repository = repository
		self._labels_by_state = labels_by_state
		self._max_distance = max_distance

		self._commits_by_id = DefaultDict(lambda k: Commit(self._repository, k))
		self._trees_by_id = DefaultDict(lambda k: Tree(self._repository, k))

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
		label = None if state is None else self._labels_by_state[state]
		current_label, current_ref = self._state_by_commit_id.get(commit_id, (None, None))

		if current_label != label:
			if label is None:
				log('Removing label of commit.', commit_id[:7])
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

	async def _check_refs(self):
		log('Reading refs ...')

		distances_by_commit_id = await self._get_reachable_commits()
		commits_to_label = sorted((k for k, v in distances_by_commit_id.items() if v < self._max_distance), key = distances_by_commit_id.get)

		async def get_available_state(tree, _commit_id_hint):
			return tree.get_result_if_available()

		async def get_state(tree, commit_id_hint):
			result = await tree.get_result(commit_id_hint)

			self._write_tox_results()

			return result

		for fn in get_available_state, get_state:
			for i in commits_to_label:
				tree_id = await self._commits_by_id[i].get_tree_id()
				state = await fn(self._trees_by_id[tree_id], i)

				await self._set_label(i, state)

		for i in list(self._state_by_commit_id):
			if i not in distances_by_commit_id:
				await self._set_label(i, None)

	def _read_tox_results(self):
		try:
			data = loads(self._repository.read_file(_tox_state_file_path))
		except FileNotFoundError:
			return

		for i in data:
			tree = self._trees_by_id[i['tree_id']]
			state = TreeState(i['state'])

			tree.set_tox_result(state)

	def _write_tox_results(self):
		def iter_data():
			for k, v in self._trees_by_id.items():
				state = v.get_result_if_available()

				if state != TreeState.pending:
					yield dict(tree_id = k, state = state.value)

		data = dumps(list(iter_data()))

		self._repository.write_file(_tox_state_file_path, data)

	async def run(self):
		"""
		Reads existing tags and keeps them updated when refs change.
		"""

		log('Initializing ...')

		await self.clear_labels()

		self._read_tox_results()

		async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
			while True:
				await self._check_refs()
				await watcher()

	async def clear_labels(self):
		for i in await self._repository.show_ref():
			if _is_label(i):
				await self._repository.delete_ref(i)
