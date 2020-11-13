import os
from asyncio import ensure_future, Event, Queue
from asyncio.tasks import gather
from collections import UserDict
from enum import Enum
from functools import partial
from itertools import count
from json import loads, dumps
from typing import NamedTuple

from git_toxic.git import Repository
from git_toxic.pytest import read_summary, get_summary_statistics
from git_toxic.util import command, DirWatcher, log, read_file, write_file, \
    cleaned_up_directory


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


class Settings(NamedTuple):
    labels_by_state: dict
    max_distance: int
    command: str
    max_tasks: int
    resultlog_path: str


class DefaultDict(UserDict):
    def __init__(self, value_fn):
        super().__init__()

        self._value_fn = value_fn

    def __missing__(self, key):
        value = self._value_fn(key)

        self[key] = value

        return value


class Labelizer:
    # It was actually really hard to find those characters! They had to be
    # rendered as zero-width space in a GUI application, not produce a line-
    # break, be considered different from each other by HFS, not normalize to
    # the empty string and not be considered a white-space character by git.
    _invisible_characters = [chr(0x200b), chr(0x2063)]

    def __init__(self, repository: Repository):
        self._repository = repository

        self._label_id_iter = count()
        self._label_by_commit_id = {}

    def _get_label_suffix(self):
        id = next(self._label_id_iter)

        return ''.join(self._invisible_characters[int(i)] for i in f'{id:b}')

    async def label_commit(self, commit_id, label):
        current_label, current_ref = self._label_by_commit_id.get(commit_id, (None, None))

        if current_label != label:
            if label is None:
                log(f'Removing label from commit {commit_id[:7]}.')
            else:
                log(f'Setting label of commit {commit_id[:7]} to {label}.')

            if current_ref is not None:
                await self._repository.delete_ref(current_ref)

            if label is None:
                del self._label_by_commit_id[commit_id]
            else:
                ref = 'refs/tags/' + label + self._get_label_suffix()

                await self._repository.update_ref(ref, commit_id)
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

        return {k: v for k, v in refs.items() if not self._is_label(k)}

    @classmethod
    def _is_label(cls, ref):
        return ref[-1] in cls._invisible_characters


class ToxicTask(NamedTuple):
    tree_id: str
    commit_id: str


class Toxic:
    def __init__(self, repository: Repository, settings: Settings):
        self._repository = repository
        self._settings = settings

        self._labelizer = Labelizer(self._repository)

        self._update_labels_event = Event()

        self._task_queue = Queue()

        # Each value is either a ToxResult instance or `...`, if a task is
        # currently queued for that commit ID.
        self._results_by_tree_id = {}

        self._commits_by_id = DefaultDict(partial(Commit, self))

    async def _get_reachable_commits(self):
        """
        Collects all commits reachable from any refs which are not created by
        this application.

        Returns a dict from commit ID to distance, where distance is the
        distance to the nearest child to which a ref points.
        """
        allowed_ref_dirs = ['heads', 'remotes']
        res = {}

        for k, v in (await self._labelizer.get_non_label_refs()).items():
            if any(k.startswith(f'refs/{i}/') for i in allowed_ref_dirs):
                for i, x in enumerate(await self._repository.rev_list(v)):
                    distance = res.get(x)

                    if distance is None or distance > i:
                        res[x] = i

        return res

    async def _worker(self, work_dir):
        while True:
            task = await self._task_queue.get()

            log(f'Running command for commit {task.commit_id[:7]} ...')

            with cleaned_up_directory(work_dir):
                await self._repository.export_to_dir(task.commit_id, work_dir)

                env = dict(
                    os.environ,
                    TOXIC_ORIG_GIT_DIR=os.path.relpath(self._repository.path, work_dir))

                result = await command(
                    'bash',
                    '-c',
                    self._settings.command,
                    cwd=work_dir,
                    env=env,
                    allow_error=True)

                if self._settings.resultlog_path is None:
                    pytest_summary = None
                else:
                    path = os.path.join(work_dir, self._settings.resultlog_path)

                    try:
                        pytest_summary = read_summary(path)
                    except FileNotFoundError:
                        log(f'Warning: Resultlog file {path} not found.')

                        pytest_summary = None

            self._results_by_tree_id[task.tree_id] = \
                ToxResult(not result.code, pytest_summary)

            self._update_labels_event.set()

    async def _get_label(self, commit_id):
        # Results are cached by the tree ID, but testing a tree requires the
        # commit ID.
        tree_id = await self._commits_by_id[commit_id].get_tree_id()
        result = self._results_by_tree_id.get(tree_id)

        if result is None:
            self._task_queue.put_nowait(ToxicTask(tree_id, commit_id))
            self._results_by_tree_id[tree_id] = result = ...

        if result is ...:
            label = self._settings.labels_by_state[TreeState.pending]
        else:
            state = TreeState.success if result.success else TreeState.failure
            label = self._settings.labels_by_state[state]
            summary = result.summary

            if summary is not None and label is not None:
                statistics = get_summary_statistics(summary)

                def iter_parts():
                    counters = [
                        ('e', statistics.errors),
                        ('f', statistics.failures),
                        ('s', statistics.skipped),
                        ('x', statistics.xpassed)]

                    for c, n in counters:
                        if n > 5:
                            yield c + str(n)
                        elif n:
                            yield c * n

                label += ''.join(_space + i for i in iter_parts())

        return label

    async def _check_refs(self):
        distances_by_commit_id = await self._get_reachable_commits()
        commit = [
            k for k, v in distances_by_commit_id.items()
            if v < self._settings.max_distance]

        labels_by_commit_id = {}

        for i in sorted(commit, key=distances_by_commit_id.get):
            labels_by_commit_id[i] = await self._get_label(i)

        await self._labelizer.set_labels(labels_by_commit_id)

    def _read_tox_results(self):
        try:
            path = os.path.join(self._repository.path, _tox_state_file_path)
            data = loads(read_file(path))
        except FileNotFoundError:
            return

        self._results_by_tree_id = {
            i['tree_id']: ToxResult(i['success'], i['summary'])
            for i in data}

    def _write_tox_results(self):
        data = [
            dict(tree_id=k, success=v.success, summary=v.summary)
            for k, v in self._results_by_tree_id.items()
            if v is not ...]

        path = os.path.join(self._repository.path, _tox_state_file_path)

        write_file(path, dumps(data))

    async def run(self):
        """
        Reads existing tags and keeps them updated when refs change.
        """
        await self._labelizer.remove_label_refs()
        self._read_tox_results()

        async def watch_dir():
            async with DirWatcher(os.path.join(self._repository.path, 'refs')) as watcher:
                while True:
                    await watcher()
                    self._update_labels_event.set()

        async def process_events():
            log('Waiting for changes ...')

            while True:
                self._write_tox_results()
                await self._check_refs()

                await self._update_labels_event.wait()
                self._update_labels_event.clear()

        worker_tasks = [
            self._worker(os.path.join(self._repository.path, f'toxic/worker-{i}'))
            for i in range(self._settings.max_tasks)]

        await gather(watch_dir(), process_events(), *worker_tasks)

    async def clear_labels(self):
        await self._labelizer.remove_label_refs()
