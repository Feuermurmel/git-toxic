import enum
from itertools import count

from git_toxic.git import Repository
from git_toxic.util import log


class TreeState(enum.Enum):
    pending = "pending"
    success = "success"
    failure = "failure"


class Labelizer:
    # It was actually really hard to find those characters! They had to be
    # rendered as zero-width space in a GUI application, not produce a line-
    # break, be considered different from each other by HFS, not normalize to
    # the empty string and not be considered a white-space character by git.
    _invisible_characters = [chr(0x200B), chr(0x2063)]

    def __init__(self, repository: Repository):
        self._repository = repository

        self._label_id_iter = count()
        self._label_by_commit_id = {}

    def _get_label_suffix(self):
        id = next(self._label_id_iter)

        return "".join(self._invisible_characters[int(i)] for i in f"{id:b}")

    async def label_commit(self, commit_id, label):
        current_label, current_ref = self._label_by_commit_id.get(
            commit_id, (None, None)
        )

        if current_label != label:
            if label is None:
                log(f"Removing label from commit {commit_id[:7]}.")
            else:
                log(f"Setting label of commit {commit_id[:7]} to {label}.")

            if current_ref is not None:
                await self._repository.delete_ref(current_ref)

            if label is None:
                del self._label_by_commit_id[commit_id]
            else:
                ref = "refs/tags/" + label + self._get_label_suffix()

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
