from argparse import ArgumentParser
from asyncio.events import get_event_loop
from contextlib import closing

from git_toxic.git import Repository
from git_toxic.toxic import Toxic, TreeState, Settings


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

    return Settings(
        labels_by_state={
            TreeState.pending: await read_label('pending', default_pending_label),
            TreeState.success: await read_label('success', None),
            TreeState.failure: await read_label('failure', default_failure_label)},
        max_distance=int(await repository.read_config('toxic.max-distance', 5)),
        command=await repository.read_config('toxic.command', 'tox'),
        max_tasks=int(await repository.read_config('toxic.max-tasks', '1')),
        resultlog_path=await repository.read_config('toxic.resultlog-path'))


async def main(clear: bool):
    repository = await Repository.from_cwd()
    settings = await read_settings(repository)
    toxic = Toxic(repository, settings)

    if clear:
        await toxic.clear_labels()
    else:
        await toxic.run()


def script_main():
    with closing(get_event_loop()) as loop:
        loop.run_until_complete(main(**vars(parse_args())))
