# Normal worker build retains the curl repair

The standard Dockerfile installs authenticated curl packages from the immutable
build input after all other apt operations, retaining licenses and provenance.
The complete normal build passed. Root descriptor:
`sha256:cb081f259e95108195c22ef7506eeed843fef6d015f7ce33366ad4f69fd8284c`.

Frozen raw scan: 24 Critical /106 High. Exact binary-bound curl review:
0 Critical /76 High. Raw findings remain intact. Installed AWS CLI model and
command-refusal checks passed, as did native curl/Git trusted TLS, wrong-host
and untrusted-certificate refusal tests. The server logged expected connection
resets during refusal fixtures; the final assertions passed.

Candidate only. Live rollout, digest binding and worker acceptance remain.
