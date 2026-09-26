# AWS CLI release verification

`aws-signing.asc` is the public key embedded in the official AWS CLI installation
guide, retrieved 2026-09-26:
https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

Its fingerprint remains `FB5DB77FD5C118B80511ADA8A6310ACC4672475C`.
AWS renewed its expiration to Unix time `1814472778`; the Ubuntu keyserver still
served metadata expiring at `1783435745`. That older metadata produced
`EXPKEYSIG` for the September 25 AWS CLI 2.37.4 signature even though GPG exited
zero. The verifier now pins the reviewed official key bytes and ZIP, verifies in
an isolated keyring, and requires GOODSIG/VALIDSIG without expiration, revocation,
or error statuses. Expiration is not waived by the ZIP checksum.

Key SHA-256: `b3cef249c50f7e26254ffd91bc7453d7424247cc98c372840e70297060c0e146`.
The key is public; no signing secret or credential is included.
