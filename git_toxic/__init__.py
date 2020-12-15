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
    async def read_label(state, default):
        value = await repository.read_config('toxic.label-' + state, default)

        if value:
            return value
        else:
            return None

    async def read_path(name, default):
        value = await repository.read_config(name)

        if value is None:
            return default
        else:
            return pathlib.Path(value).expanduser()

    labels_by_state = {
        TreeState.pending: await read_label('pending', default_pending_label),
        TreeState.success: await read_label('success', None),
        TreeState.failure: await read_label('failure', default_failure_label)}
    max_distance = int(await repository.read_config('toxic.max-distance', 5))
    work_dir = await read_path('toxic.work-dir', pathlib.Path(repository.path) / 'toxic')
    command = await repository.read_config('toxic.command')
    max_tasks = int(await repository.read_config('toxic.max-tasks', 1))
    summary_path = await repository.read_config('toxic.summary-path')
    history_limit = await repository.read_config('toxic.history-limit')

    if command is None:
        raise UserError('No value for configuration toxic.command has been set.')

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

