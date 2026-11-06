# Fixed management-bootstrap diagnostic provenance

This qualifies only `superplane-paid-worker-6nf6p-2gs8d`, UID
`84752a30-588b-407f-989c-9545da6522d3`, from image `845ce1dc…` and source
`0025522e4a21c5373aa24aa3f0473ccbef4d5f66`. It is not execution permission.
The exact protected audit contains 55 exec sessions, 110 stream stages, four
programs and eight pod lifecycle mutations. Missing, extra, changed or replayed
events refuse qualification. No attach or ephemeral-container session qualifies.

The four programs read this operation's credential-free journal fields and
process/file names. Their only application call is `operation_directory` with
its default `create=False`; imports and that path were inspected in the pinned
image. No program executes a subprocess, sends a request, opens a writable file,
or consumes stdin. HTTP 101 alone does not prove execution or success.

The pinned image was inspected offline with networking disabled and a read-only
root filesystem. `python` resolves through the fixed PATH to
`/opt/executor/bin/python` (`/usr/local/bin/python3.12`); image working directory
is `/`. Python has no `.pth`, `sitecustomize` or `usercustomize` files, user site
is disabled, and imports resolve to the immutable image. The pod container and
volume fingerprints fix its non-root user, read-only root, working directory,
environment and mount layout; no writable mount shadows the interpreter, import
paths or `/`. The current ConfigMap has no Python or PATH overrides. Its exact
UID/version/data hash and complete mutation-free history are checked before
publication. The image's full provisioning/bootstrap source manifest separately
pins the imported application sources, including `terraform`, `artifacts`,
`runtime_config` and `execution_contract`.

For this one pod, collection reads every mutation and exec/attach/ephemeral
container event through actual now. It matches both objectRef and response name
so generated-name creation is retained. Ordinary get/list/watch responses cannot
mutate the source; omitting them prevents high-frequency observers exhausting
the event bound. All eight mutation records are pinned by protected event ID and
credential-free request/response fingerprints, including lifecycle, binding,
status, finalizer removal and deletion. Creation and terminal object identities
and all source container/volume documents must agree.

Publish owner evidence before changing the runtime ConfigMap. This finite case
does not accept the older worker's historical-environment exception or extend
any authority, retry allowance, lease, journal or settlement.
