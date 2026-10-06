# Python 3.12 maintenance with command preservation

Updating util-linux and login without their new companion packages removes commands from the official Python base. This recipe starts from the exact official Python 3.12.15 Debian 13 platform, installs the previously reviewed 15 maintenance packages, and explicitly adds the matching `util-linux-extra` and `bsdextrautils` packages. Seven compatibility links preserve original `/usr/sbin` command paths after Debian moved their fixed implementations to `/usr/bin`.

The artifact lock records original and selected package versions, architecture and SHA-256. Stage the independently reviewed local build artifacts with `python3 prepare.py --package-directory /path/to/reviewed/packages` (repeat the directory argument as needed), then build this directory with its Dockerfile. Artifacts are omitted from source control. The install has no network access and checks the full package inventory, unchanged Python distributions, every original executable path, dpkg audit and pip dependencies. Exactly two package additions are expected; no removals or unrelated upgrades are allowed.

The selected ACL, glibc, ncurses and util-linux payloads require their authenticated source, build and independent security review records. Source hashes alone are not a security approval. Qualification must independently compare final payloads and original command paths, run ordinary runtime checks, retain fresh raw scanning evidence, and review exact occurrences. No release pin, registry publication, scanner suppression or deployment is performed here.

The upstream image has implicit root as its build user; this recipe records equivalent explicit UID 0. Application Dockerfiles must select their own nonroot runtime users.
