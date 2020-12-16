import os
import pathlib
from asyncio import ensure_future, Event
from asyncio.queues import PriorityQueue
from asyncio.tasks import gather
from collections import UserDict
from functools import partial
from json import loads, dumps
from math import inf
from typing import NamedTuple, Optional, List

from git_toxic.git import Repository
from git_toxic.labels import Labelizer, TreeState
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


class ToxicResult(NamedTuple):
    success: bool
    summary: Optional[str]


class Settings(NamedTuple):
    labels_by_state: dict
    max_distance: Optional[int]
    work_dir: pathlib.Path
    command: str
    max_tasks: int
    summary_path: Optional[str]
    history_limit: List[str]


class DefaultDict(UserDict):
    def __init__(self, value_fn):
        super().__init__()

        self._value_fn = value_fn

    def __missing__(self, key):
        value = self[key] = self._value_fn(key)

        return value


class ToxicTask(NamedTuple):
    distance: int
    tree_id: str
    commit_id: str


class Toxic:
    def __init__(self, repository: Repository, settings: Settings):
        self._repository = repository
        self._settings = settings

        self._labelizer = Labelizer(self._repository)

        self._update_labels_event = Event()

        self._task_queue = PriorityQueue()

        # Each value is either a ToxResult instance or `...`, if a task is
        # currently queued for that commit ID.
        self._results_by_tree_id = {}

        self._commits_by_id = DefaultDict(partial(Commit, self))

    async def _get_reachable_commits(self):
        """
        Collects all commits reachable from any refs which are not created by
        this application.

        Returns a list of tuples (commit id, distance), where distance is the
        distance to the nearest child to which a ref points.
        """
        allowed_ref_dirs = ['heads', 'remotes']
        distances = {}

        for k, v in (await self._labelizer.get_non_label_refs()).items():
            if any(k.startswith(f'refs/{i}/') for i in allowed_ref_dirs):
                def iter_rev_list_args():
                    if self._settings.history_limit:
                        yield '--ancestry-path'

                    yield v

                    # Exclude commits from which the history limit commits are
                    # not reachable.
                    for i in self._settings.history_limit:
                        yield f'^{i}'

                for i, x in enumerate(
                        await self._repository.rev_list(*iter_rev_list_args())):
                    # TODO: The index is not really the distance when merges
                    #  are involved.
                    distances[x] = min(distances.get(x, inf), i)

        return [*distances.items()]

    async def _run_command(self, work_dir, commit_id):
        log(f'Running command for commit {commit_id[:7]} ...')

        with cleaned_up_directory(work_dir):
            await self._repository.export_to_dir(commit_id, work_dir)

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

            summary_path = os.path.join(work_dir, self._settings.summary_path)

            try:
                summary = read_file(summary_path)
            except FileNotFoundError:
                log(f'Warning: Summary file {summary_path} not found.')

                summary = None

            return ToxicResult(not result.code, summary)

    async def _worker(self, work_dir):
        while True:
            task = await self._task_queue.get()

            # Only run the task if it has not yet been cancelled or already
            # run (a task may get scheduled multiple times).
            if self._results_by_tree_id.get(task.tree_id) is ...:
                result = await self._run_command(work_dir, task.commit_id)

                self._results_by_tree_id[task.tree_id] = result
                self._update_labels_event.set()

    def _get_label(self, result):
        if result is ...:
            label = self._settings.labels_by_state[TreeState.pending]
        else:
            state = TreeState.success if result.success else TreeState.failure
            label = self._settings.labels_by_state[state]
            summary = result.summary

            if label is not None and summary is not None:
                label = _space.join([label, *summary.split()])

        return label

    async def _apply_labels(self):
        labels_by_commit_id = {}
        seen_tree_ids = set()

        for commit_id, distance in await self._get_reachable_commits():
            if self._settings.max_distance is None \
                    or distance < self._settings.max_distance:
                tree_id = await self._commits_by_id[commit_id].get_tree_id()
                seen_tree_ids.add(tree_id)
                result = self._results_by_tree_id.get(tree_id)

                if result is None:
                    # Results are cached by the tree ID, but testing a tree
                    # requires the commit ID.
                    self._task_queue.put_nowait(
                        ToxicTask(distance, tree_id, commit_id))

                    result = self._results_by_tree_id[tree_id] = ...

                labels_by_commit_id[commit_id] = self._get_label(result)

        # Remove entries for commits that we don't want to be labelled anymore
        # so that the tasks are skipped.
        for i in set(self._results_by_tree_id) - seen_tree_ids:
            if self._results_by_tree_id[i] is ...:
                del self._results_by_tree_id[i]

        await self._labelizer.set_labels(labels_by_commit_id)

    def _read_tox_results(self):
        try:
            path = os.path.join(self._repository.path, _tox_state_file_path)
            data = loads(read_file(path))
        except FileNotFoundError:
            return

        self._results_by_tree_id = {
            i['tree_id']: ToxicResult(i['success'], i['summary'])
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
                await self._apply_labels()

                await self._update_labels_event.wait()
                self._update_labels_event.clear()

        worker_tasks = [
            self._worker(str(self._settings.work_dir / f'worker-{i}'))
            for i in range(self._settings.max_tasks)]

        await gather(watch_dir(), process_events(), *worker_tasks)

    async def clear_labels(self):
        await self._labelizer.remove_label_refs()
