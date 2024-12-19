from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from git_toxic.util import CommandResult
from git_toxic.util import command
from git_toxic.util import command_lines


class Repository:
    def __init__(self, path: str):
        self.path = path

    def _command_args_prefix(self) -> list[str]:
        return ["git", "--git-dir", self.path]

    async def _command(self, cmd: list[str], **kwargs: Any) -> CommandResult:
        return await command([*self._command_args_prefix(), *cmd], **kwargs)

    async def _command_lines(self, cmd: list[str], **kwargs: Any) -> list[str]:
        return await command_lines([*self._command_args_prefix(), *cmd], **kwargs)

    async def rev_list(self, *refs: str) -> list[str]:
        return await self._command_lines(["rev-list", *refs])

    async def show_ref(self) -> dict[str, str]:
        list = await self._command_lines(["show-ref"])

        def iter_entries() -> Iterator[tuple[str, str]]:
            for i in list:
                parts = i.split(" ", 1)

                # Ignore entries with line breaks in them.
                if len(parts) == 2:
                    k, v = parts

                    yield v, k

        return dict(iter_entries())

    async def get_commit_info(self, commit_id: str) -> dict[str, str]:
        lines = await self._command_lines(["cat-file", "commit", commit_id])

        def iter_entries() -> Iterator[list[str]]:
            for i in lines:
                if not i:
                    return

                yield i.split(" ", 1)

        return dict(iter_entries())

    async def update_ref(self, name: str, commit_id: str) -> None:
        await self._command(["update-ref", name, commit_id])

    async def delete_ref(self, name: str) -> None:
        await self._command(["update-ref", "-d", name])

    async def read_config(self, name: str) -> str | None:
        result = await self._command_lines(["config", name], allow_error=True)

        if result:
            (value,) = result

            return value
        else:
            return None

    async def clone_to_dir(self, commit_id: str, dir: str) -> None:
        Path(dir).mkdir(parents=True, exist_ok=True)

        await command(["git", "init"], cwd=dir)
        await command(["git", "fetch", "-n", self.path, commit_id], cwd=dir)
        await command(["git", "checkout", "-fq", commit_id], cwd=dir)
        await command(["git", "clean", "-df"], cwd=dir)

    @classmethod
    async def from_dir(cls, path: str) -> Repository:
        (line,) = await command_lines(["git", "rev-parse", "--git-dir"], cwd=path)

        return cls(os.path.abspath(line))

    @classmethod
    async def from_cwd(cls) -> Repository:
        return await cls.from_dir(".")
