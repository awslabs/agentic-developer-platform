# python-jose compatibility maintenance

This historical API compatibility recipe preserves its application and schema.
Its checksum-pinned python-jose 3.5.0 source is maintained locally as
`3.5.0+adp2`. The existing cryptography-only EC backend change remains in place.

[GHSA-3qf3-8w2g-rqmx](https://github.com/advisories/GHSA-3qf3-8w2g-rqmx)
(CVE-2026-85394) describes accepting DER public keys as HMAC secrets. The
advisory lists no upstream patched release. `patch_jose.py` adds a shared
DER public-key check to both native and cryptography HMAC backends. It uses the
already-required cryptography parser, preserves opaque symmetric secrets, and
rejects recognized unsupported asymmetric algorithms. The patch refuses an
unexpected upstream HMAC implementation.

`verify_jose_der.py` exercises RSA SPKI/PKCS1, EC and Ed25519 DER public keys
against HS256/384/512, both HMAC backends and forged JWT verification. It also
accepts string/binary HMAC secrets, non-key ASN.1 bytes, explicit symmetric JWKs,
and normal RSA/EC signatures. The test fails on the preceding `+adp1` image with
`DER public-key forgery accepted`; the final image runs it as UID 65532 during
every build. Existing wrong-audience, invalid-signature and tar extraction
regressions remain required.

The local version suffix is provenance, not a scanner suppression. Retain raw
scanner matches and bind any patched-package disposition to the exact installed
source hashes and successful regression output. This recipe does not promote
the historical schema to a current production API release.
