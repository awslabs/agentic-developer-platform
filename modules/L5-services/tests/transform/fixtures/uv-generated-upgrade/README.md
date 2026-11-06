# Real uv lock upgrade fixture

Generated with uv 0.13.5 and Python 3.12 using `uv lock --index-url
https://pypi.org/simple`. The candidate changes the project's exact requirement
from `packaging==24.2` to `packaging==25.0` and regenerates the lock with the same
command. The package URLs and artifact hashes came from PyPI. No package source,
private repository, credentials, or native provider result is included.

Both locks contain uv's virtual root package. Its `metadata.requires-dist`
version must change with the selected dependency, while every other root field
and non-target package remains unchanged. These files test the independent
dependency parser; they are not evidence that Transform performed an upgrade.
