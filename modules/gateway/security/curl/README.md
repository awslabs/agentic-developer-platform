# Debian curl cookie and proxy credential repairs

This bundle rebuilds the gateway's existing Debian curl 8.14.1 flavor with the
upstream fix for CVE-2026-8924. A cookie scoped to `co.uk.` must not be accepted
from `foo.co.uk.` and sent to unrelated `bar.co.uk.`. The synthetic regression
also checks that an ordinary same-origin cookie continues to work.

The patch comes from merged upstream commit
[51beed175dbfc37da3113f6acce60c630c070ce8](https://github.com/curl/curl/commit/51beed175dbfc37da3113f6acce60c630c070ce8).
Its runtime-library hunk is unchanged. Test 1629 retains the same cookie
assertions, with the older harness's `crlf="yes"` spelling; the Makefile test-list
registration is adapted to the older release's test 1621 endpoint. Patching uses
zero fuzz and verifies exact before/after `lib/cookie.c` hashes.

`source-lock.json` pins the signed Debian 8.14.1-2+deb13u5 descriptor and every
source artifact by SHA256. The original descriptor's signature and artifact
hashes were independently verified with the Debian keyring. The package version
is explicitly local: `8.14.1-2+deb13u5+adp2`. Debian's configure options, hardening
and ABI packaging remain in force. The official `pkg.curl.openssl-only` profile
builds the OpenSSL flavor already used by the gateway, including HTTP3; it does
not disable runtime protocols or features.

Compilation and Debian's nonflaky test suite run with networking disabled after
fetching build dependencies. Only the curl and libcurl4t64 packages are copied
into the runtime. The runtime's additional cookie regression runs as UID 65532
with networking disabled. Build tools and source downloads stay in the builder.

Curl rates this advisory **Low**; the frozen scanner reports **Critical**. Keep
both ratings rather than replacing one with the other. Other curl findings,
Python ZIP/POP3 gaps and the wider #6112 acceptance remain open. This repair
implies no publication, gateway/tick rollout or blanket risk acceptance.

## Proxy credential clearing: CVE-2026-9079

The second patch applies merged upstream commit
[88c7e16cceec816a2df45c899d49b1e85513f193](https://github.com/curl/curl/commit/88c7e16cceec816a2df45c899d49b1e85513f193).
Calling `CURLOPT_PROXYUSERPWD` with NULL must discard both stored proxy credentials.
The previous code retained them and sent them on later transfers. The runtime
hunk only maps upstream's newer `s->str`/`curlx_safefree` names to the existing
`data->set.str`/`Curl_safefree`; its control flow and clearing behavior are identical.
Exact before/after `lib/setopt.c` hashes guard this adaptation.

Upstream test 1648 retains both requests and its expected authorization headers.
Compatibility changes are older harness includes, `test(char *)` entry point,
`res` macro variable, Makefile registration, `crlf="yes"`, and literal base64 for
the synthetic credential. `check-proxy.py` independently exercises the installed
libcurl through ctypes with a loopback proxy, checks credential presence before
and after restoration, and rejects any credential header after clearing. It uses
only synthetic credentials and runs without external networking as UID 65532.

Curl rates this advisory **Medium**, while the frozen scanner reports **Critical**.
The vendor says the curl command-line interface is unaffected; the installed
libcurl API is affected and therefore needs this repair. Both original frozen
package occurrences remain recorded; source repair is separate from scanner
finding disappearance and live workload acceptance.
