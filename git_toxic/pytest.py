from git_toxic.util import read_file


def read_summary(path):
	def iter_chars():
		for i in read_file(path).splitlines():
			yield i[:min(1, len(i))].strip()

	return ''.join(iter_chars())


class Statistics:
	def __init__(self, errors, failures):
		self.errors = errors
		self.failures = failures


def get_summary_statistics(summary: str):
	errors = 0
	failures = 0

	for i in summary:
		if i == 'E':
			errors += 1
		elif i == 'F':
			failures += 1

	return Statistics(errors, failures)
