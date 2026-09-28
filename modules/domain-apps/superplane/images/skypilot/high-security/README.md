# SkyPilot dependency maintenance

This overlay retains SkyPilot0.12.3 and the authenticated deployment contract. The final UID/GID1000 matches the existing deployment security context.

The pinned maintenance inputs replace vulnerable Go helper binaries, Pillow, Ray and its isolated aiohttp. VastAI1.0.13 source is unchanged except tested Pillow12.3.0 and cryptography46.0.7 dependency pins and a local package version. Cryptography46.0.7+adp1 includes the upstream certificate-chain budget and PKCS#7 uniform-error fixes, and links to the existing fixed systemOpenSSL3.5.7. Both security patches retain upstream provenance; only source hunks are applied with zero fuzz. The Rust path-verification tests and valid/invalid RSA/ECDSA/Fernet/PKCS#7 checks execute during the build.

CPython3.10.21 retains its ABI. Its XML adapters are rebuilt against Expat2.8.5 with the upstream128-bit hash-salt interface backport; both adapters and source hashes are recorded and the upstream XML suites pass. The tarfile hardlink resolution fix includes an extraction regression. The early Debian maintenance artifact supplies tested ACL2.4.0, Expat2.8.5 and patched Perl modules only; glibc, ncurses and util-linux changes are excluded pending validation.

Ray's unsigned JAR replaces unshaded HttpCore5.0.2 with checksum-pinned5.4.3. Other entries retain byte hashes, and an actual Java response-parser regression fails on the original JAR and passes on the replacement.

Build from this directory with Docker. All base/tool images and external source artifacts are immutable/checksum pinned. This recipe is a candidate until publication receipts and live runtime verification establish deployment. Remaining OS/OpenSSH findings stay open; raw scanner output is retained alongside exact candidate-bound maintenance reviews.
