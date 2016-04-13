import os
import random
from asyncio import ensure_future, Future
from tempfile import TemporaryDirectory

from git_toxic.util import command, DirWatcher
from git_toxic.git import Repository


# It was actually really hard to find those characters! They had to be rendered as zero-width space in a GUI application, not produce a line-break, be considered different from each other by HFS, not normalize to the empty string and not considered a white-space character by git.
invisible_characters = [chr(0x200b), chr(0x2063)]


def id_to_invisible(commit_id: str):
	bits = '{:0160b}'.format(int(commit_id, 16))

	return ''.join(invisible_characters[int(i)] for i in bits[:30])




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
				return await run_tox_on_tree(self.repository, self.tree_id)

			self._tox_result_future = ensure_future(get())

		return await self._tox_result_future


class Toxic:
	def __init__(self, repository: Repository, success_tag, failure_tag):
		self._repository = repository
		self._success_tag = success_tag
		self._failure_tag = failure_tag

		self._tag_refs = set()
		self._tagged_commit_ids = set()
		self._commits_by_commit_ids = { }
		self._trees_by_tree_ids = { }

	def _get_commit(self, commit_id):
		if commit_id not in self._commits_by_commit_ids:
			self._commits_by_commit_ids[commit_id] = Commit(self._repository, commit_id)

		return self._commits_by_commit_ids[commit_id]

	def _get_tree(self, tree_id):
		if tree_id not in self._trees_by_tree_ids:
			self._trees_by_tree_ids[tree_id] = Tree(self._repository, tree_id)

		return self._trees_by_tree_ids[tree_id]

	def _find_unused_tag_ref(self, prefix):
		while True:
			name = prefix + ''.join(random.choice(invisible_characters) for _ in range(30))

			if name not in self._tag_refs:
				return name

	async def _check_refs(self):
		refs = await self._repository.show_ref()
		head_revs = [v for k, v in refs.items() if k[-1] not in invisible_characters]

		def iter_revs():
			*head_revs

		for i in await self._repository.rev_list(*head_revs):
			if i not in self._tagged_commit_ids:
				print(i)

				tree_id = await self._get_commit(i).get_tree_id()
				result = await self._get_tree(tree_id).get_tox_result()
				prefix = self._success_tag if result.success else self._failure_tag
				name = self._find_unused_tag_ref('refs/tags/' + prefix)

				await self._repository.update_ref(name, i)

				self._tag_refs.add(name)
				self._tagged_commit_ids.add(i)

	async def _read_existing_tags(self):
		for k, v in (await self._repository.show_ref()).items():
			if k[-1] in invisible_characters:
				tree = self._get_tree(await self._get_commit(v).get_tree_id())
				success = k.startswith(self._success_tag)

				tree.set_tox_result(ToxResult(success))

				self._tag_refs.add(k)
				self._tagged_commit_ids.add(v)

	async def run(self):
		await self._read_existing_tags()

		async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
			while True:
				print(os.path.join(self._repository.path, 'refs'))
				await self._check_refs()
				await watcher()
