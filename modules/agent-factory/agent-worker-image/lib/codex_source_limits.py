"""Host-owned source bounds shared by materialization and detached validation.

Compressed provider transfers keep their separate gateway limit. Expanded trees
fit the validation backend's 512 MiB workspace with room for build outputs.
These constants are not model-selectable or inherited from repository config.
"""

MAX_SOURCE_ENTRIES = 10000
MAX_SOURCE_BYTES = 192 * 1024 * 1024
# Tar headers/padding and directory records also consume space.
MAX_VALIDATION_ARCHIVE_BYTES = 208 * 1024 * 1024
MAX_PROVIDER_ARCHIVE_BYTES = 64 * 1024 * 1024
