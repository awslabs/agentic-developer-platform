# Schema016 API compatibility maintenance

This overlay preserves the live schema016 application tree and applies dependency maintenance only. It is separate from the newer normal API release pin.

The same python-jose3.5.0 source is packaged as3.5.0+adp1 with the cryptography backend required and the vulnerable pure-Python ECDSA fallback removed. HS256/RS256/ES256 signatures, invalid signatures and invalid audiences are tested. The tarfile hardlink fix is bound to before/after source hashes. The early tested Debian artifact supplies ACL/Expat/Perl fixes and ABI-matched Python3.12.14 XML adapters.

The application tree hash before and after is617f890b345fbe5d8a73b2d380f4e03e64fbdcdf7247f2f0fb10d82a9bb018e6. Application token roundtrips and trusted/untrusted TLS tests pass. Runtime identity remains65532:65532. Further OS findings remain open pending validated repairs.
