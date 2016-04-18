from argparse import ArgumentParser
from asyncio.events import get_event_loop
from contextlib import closing

from git_toxic.git import Repository
from git_toxic.toxic import Toxic, TreeState, Settings


yes = chr(0x1f3Be) # Tennis ball
no = chr(0x274c) # Red cross mark

check_mark = chr(0x2714)
cross_mark = chr(0x2718)

colon = chr(0xa789)
space = chr(0xa0)
dots = chr(0x22ef)


def parse_args():
	parser = ArgumentParser()

	parser.add_argument('--clear', action = 'store_true')

	return parser.parse_args()


async def read_settings(repository: Repository):
	async def read_label(state, default):
		value = await repository.read_config('toxic.label-' + state, default)

		if value:
			return value
		else:
			return None

	return Settings(
		labels_by_state = {
			TreeState.pending: await read_label('pending', dots),
			TreeState.success: await read_label('success', check_mark),
			TreeState.failure: await read_label('failure', cross_mark) },
		max_distance = int(await repository.read_config('toxic.max-distance', 5)),
		command = await repository.read_config('toxic.command', 'tox'),
		max_tasks = int(await repository.read_config('toxic.max-tasks', '4')))


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
