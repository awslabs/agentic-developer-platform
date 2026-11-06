# LiteLLM transport correction

LiteLLM 1.102.1 silently retries a POST after `RemoteProtocolError` or
`ConnectError`, even when the request sets `num_retries=0` and `max_retries=0`.
A lost response can follow a paid Bedrock invocation. Returning only the retry's
usage then understates the operation's cost.

The image applies `single-attempt-http.py` at build time to remove that hidden
transport retry. Explicit router retry policy remains available; metered
Knowledge search disables it and all fallback lists. The correction checks the
exact upstream source hash and runs the installed HTTP handler against simulated
lost acknowledgements. A dependency update must review this correction.

Deploy and finish rolling out the updated LiteLLM and Knowledge reader images
before deploying the Gateway that exposes content search. The existing
`agent-context-images-build.yml` and `agent-context-deploy.yml` workflows cover
these images. The transport check can also run inside the deployed proxy without
credentials or provider calls:

```sh
python /opt/adp/single-attempt-http.py --verify-only
```
