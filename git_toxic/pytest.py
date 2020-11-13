from collections import Counter

from git_toxic.util import read_file


def read_summary(path):
    def iter_chars():
        for i in read_file(path).splitlines():
            yield i[:min(1, len(i))].strip()

    return ''.join(iter_chars())


class Statistics:
    def __init__(self, errors, failures, skipped, xpassed):
        self.errors = errors
        self.failures = failures
        self.skipped = skipped
        self.xpassed = xpassed


def get_summary_statistics(summary: str):
    types = dict(E='errors', F='failures', s='skipped', X='xpassed')
    counter = Counter()

    for i in summary:
        counter[types.get(i)] += 1

    return Statistics(**{i: counter[i] for i in types.values()})
