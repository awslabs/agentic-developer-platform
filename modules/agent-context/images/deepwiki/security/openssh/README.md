# OpenSSH Critical repair

CVE-2026-60002 remains a native Grype Critical match. This client backport applies upstream commit `e8bdfb151a356d0171fea4194dd205fbb252be23` to the authenticated Debian source, retaining Debian GSSAPI and hardening. Only revision banners and the GSSAPI-interrupted hunk context differ. Package identity is truthfully `1:10.0p1-7+deb13u4+adp.security.1`. Debian source itself uses the `10.0p2` binary banner despite its `10.0p1` package version.

The build proves the InRelease signature through the immutable base Debian keyring, then the Sources hash and exact source archive hashes. Debian package compilation and native unit/compatibility tests run with networking disabled. No SSH server is installed in the final image.

`validation-20260926.json` binds the compiled four-file patch, package, full combined image and scanner artifacts to source commit a76bf7cbae7cf712c7eca4385d069a3e127ead73. Subsequent evidence-only commits do not change the built runtime. Raw scanner reports stay in the local receipt paths; selected Critical/High identities and original severities remain visible.

The final image passed disposable loopback SSH key, ordinary rekey, Git clone/fetch/push, SCP/SFTP and refusal fixtures, plus existing DeepWiki tar/cold-SWC, API/UI/Git/cache, stdlib and Node-hostname tests. These checks use no production service or operator credentials and are not live acceptance. The scanner still records 25 Critical and 126 High matches: no suppression or blanket clearance is claimed. Deployment holds and parent #6122 remain in force.
