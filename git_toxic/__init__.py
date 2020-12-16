import pathlib
import sys
from argparse import ArgumentParser
from asyncio.events import get_event_loop
from contextlib import closing

from git_toxic.git import Repository
from git_toxic.toxic import Toxic, TreeState, Settings
from git_toxic.util import UserError, log


# Successful commits are not labelled by default.
default_failure_label = '\U0001F53A'
default_pending_label = '\u2022\u2005\u2022\u2005\u2022'


def parse_args():
    parser = ArgumentParser()

    parser.add_argument('--clear', action='store_true')

    return parser.parse_args()


async def read_settings(repository: Repository):
    async def read(name, type, default=...):
        value = await repository.read_config(name)

        if value is not None:
            return type(value)
        elif default is ...:
            raise UserError(f'No value for configuration {name} has been set.')
        else:
            return default

    async def read_label(state, default):
        return await read('toxic.label-' + state, str, default)

    async def read_path(name, default):
        return (await read(name, pathlib.Path, default)).expanduser()

    labels_by_state = {
        TreeState.pending: await read_label('pending', default_pending_label),
        TreeState.success: await read_label('success', None),
        TreeState.failure: await read_label('failure', default_failure_label)}
    max_distance = await read('toxic.max-distance', int, 5)
    work_dir = await read_path('toxic.work-dir', pathlib.Path(repository.path) / 'toxic')
    command = await read('toxic.command', str)
    max_tasks = await read('toxic.max-tasks', int, 1)
    summary_path = await read('toxic.summary-path', str, None)
    history_limit = await read('toxic.history-limit', str, None)

    return Settings(
        labels_by_state,
        max_distance,
        work_dir,
        command,
        max_tasks,
        summary_path,
        history_limit)


async def main(clear: bool):
    repository = await Repository.from_cwd()
    settings = await read_settings(repository)
    toxic = Toxic(repository, settings)

    if clear:
        await toxic.clear_labels()
    else:
        await toxic.run()


def script_main():
    try:
        with closing(get_event_loop()) as loop:
            loop.run_until_complete(main(**vars(parse_args())))
    except KeyboardInterrupt:
        log('Operation interrupted.')
        sys.exit(1)
    except UserError as e:
        log(f'error: {e}')
        sys.exit(2)
