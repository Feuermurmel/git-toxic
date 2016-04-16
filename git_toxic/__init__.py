from argparse import ArgumentParser
from asyncio.events import get_event_loop
from contextlib import closing

from git_toxic.git import Repository
from git_toxic.toxic import Toxic, TreeState


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


async def main(clear: bool):
	labels = {
		TreeState.pending: dots,
		TreeState.success: check_mark,
		TreeState.failure: cross_mark }

	repository = await Repository.from_cwd()
	toxic = Toxic(repository, labels, 5)

	if clear:
		await toxic.clear_labels()
	else:
		await toxic.run()


def script_main():
	with closing(get_event_loop()) as loop:
		loop.run_until_complete(main(**vars(parse_args())))
