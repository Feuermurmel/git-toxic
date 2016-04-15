import os
import random
from asyncio import ensure_future, Future
from tempfile import TemporaryDirectory

from git_toxic.util import command, DirWatcher
from git_toxic.git import Repository


# It was actually really hard to find those characters! They had to be rendered as zero-width space in a GUI application, not produce a line-break, be considered different from each other by HFS, not normalize to the empty string and not considered a white-space character by git.
invisible_characters = [chr(0x200b), chr(0x2063)]


def _is_label(ref):
	return ref[-1] in invisible_characters


async def run_tox(dir):
	result = await command('tox', cwd = dir, use_stdout = True, allow_error = True)

	return result.code == 0


async def run_tox_on_tree(repository, tree_id):
	with TemporaryDirectory() as temp_dir:
		await repository.export_to_dir(tree_id, temp_dir)

		return ToxResult(await run_tox(temp_dir))


class ToxResult:
	def __init__(self, success: bool):
		self.success = success


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


class Tree:
	def __init__(self, repository: Repository, tree_id):
		self.repository = repository
		self.tree_id = tree_id
		self._tox_result_future = None

	def set_tox_result(self, result):
		self._tox_result_future = Future()
		self._tox_result_future.set_result(result)

	async def get_tox_result(self):
		if self._tox_result_future is None:
			async def get():
				print('tox:', self.tree_id)

				return await run_tox_on_tree(self.repository, self.tree_id)

			self._tox_result_future = ensure_future(get())

		return await self._tox_result_future


class Toxic:
	def __init__(self, repository: Repository, success_tag, failure_tag, max_distance):
		self._repository = repository
		self._success_tag = success_tag
		self._failure_tag = failure_tag
		self._max_distance = max_distance

		self._commits_by_commit_ids = { }
		self._trees_by_tree_ids = { }

		self._commit_ids_by_label = { }
		self._labels_by_commit_id = { }

	def _get_commit(self, commit_id):
		if commit_id not in self._commits_by_commit_ids:
			self._commits_by_commit_ids[commit_id] = Commit(self._repository, commit_id)

		return self._commits_by_commit_ids[commit_id]

	def _get_tree(self, tree_id):
		if tree_id not in self._trees_by_tree_ids:
			self._trees_by_tree_ids[tree_id] = Tree(self._repository, tree_id)

		return self._trees_by_tree_ids[tree_id]

	def _find_unused_label(self, prefix):
		while True:
			name = prefix + ''.join(random.choice(invisible_characters) for _ in range(30))

			if name not in self._commit_ids_by_label:
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

	async def _set_label(self, commit_id, prefix):
		label = self._find_unused_label('refs/tags/' + prefix)

		await self._repository.update_ref(label, commit_id)
		self._commit_ids_by_label[label] = commit_id
		self._labels_by_commit_id[commit_id] = label

	async def _remove_label(self, commit_id):
		label = self._labels_by_commit_id[commit_id]

		await self._repository.delete_ref(label)
		del self._commit_ids_by_label[label]
		del self._labels_by_commit_id[commit_id]

	async def _check_refs(self):
		distances_by_commit_id = await self._get_reachable_commits()
		commits_to_label = [(v, k) for k, v in distances_by_commit_id.items() if k not in self._labels_by_commit_id and v < self._max_distance]

		for _, k in sorted(commits_to_label):
			tree_id = await self._get_commit(k).get_tree_id()
			result = await self._get_tree(tree_id).get_tox_result()
			prefix = self._success_tag if result.success else self._failure_tag

			await self._set_label(k, prefix)

		for i in list(self._labels_by_commit_id):
			if i not in distances_by_commit_id:
				await self._remove_label(i)

	async def _read_existing_labels(self):
		refs = await self._repository.show_ref()

		for k, v in refs.items():
			if _is_label(k):
				tree = self._get_tree(await self._get_commit(v).get_tree_id())
				success = k.startswith(self._success_tag)

				tree.set_tox_result(ToxResult(success))

				self._commit_ids_by_label[k] = v
				self._labels_by_commit_id[v] = k

	async def run(self):
		"""
		Reads existing tags and keeps them updated when refs change.
		"""
		await self._read_existing_labels()

		async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
			while True:
				await self._check_refs()
				await watcher()

	async def clear_labels(self):
		await self._read_existing_labels()

		for i in list(self._labels_by_commit_id):
			await self._remove_label(i)
