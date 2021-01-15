import setuptools


setuptools.setup(
    name='git-toxic',
    version='0.1',
    packages=['git_toxic'],
    install_requires=[
        'pyfswatch @ git+https://github.com/paul-nameless/pyfswatch'],
    entry_points=dict(
        console_scripts=[
            'git-toxic = git_toxic:script_main']))
