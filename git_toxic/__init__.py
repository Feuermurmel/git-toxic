import asyncio

import sys
from contextlib import closing

from git_toxic.git import Repository
from git_toxic.toxic import Commit, Toxic
from git_toxic.util import command, command_lines

yes = chr(0x1f3Be) # Tennis ball
no = chr(0x274c) # Red cross mark
colon = chr(0xa789)
space = chr(0xa0)


async def main():
	repository = await Repository.from_cwd()


	await Toxic(repository, yes, no).run()

	# commits = [Commit(repository, i) for i in await repository.rev_list()]

	# for i in commits:
	# 	print(await i.get_tree_id())

		# # success = repository.run_tox(i)
		# success = False
		# symbol = yes if success else no
		#
		# name = 'refs/tags/t{}{}{}{}'.format(colon, space, symbol, id_to_invisible(i))
		# # print(repr(name))
		#
		# # await repository.update_ref(name, i)
		#
		# # print(await repository.get_commit_info(i))
		#
		# print(i, (await repository.get_commit_info(i))['tree'])


def script_main():
	with closing(asyncio.get_event_loop()) as loop:
		loop.run_until_complete(main(*sys.argv[1:]))
