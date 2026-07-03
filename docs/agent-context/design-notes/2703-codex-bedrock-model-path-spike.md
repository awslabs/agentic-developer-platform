# Spike Note: Codex CLI → Bedrock model path (model ID, CLI version, auth mode)

**Issue:** #2703 (sub of EPIC #2702)
**Date:** 2026-07-03
**Status:** Complete — **GO** (verified end-to-end)
**Author:** @agent-developer

---

## 1. Summary / Go-No-Go

**GO** for the primary path: **Codex CLI `0.142.5`** → **`openai.gpt-5.5`** on the
**`bedrock-mantle`** endpoint, authenticated by the **pod's IRSA/ScaledJob role via the
AWS SigV4 credential chain — zero secrets**. This was validated end-to-end from the
ScaledJob role a KEDA pod actually runs under: a headless `codex exec` returned correct
code with exit 0 and no interactive prompts.

Decisions for downstream stories:

| Question | Answer (verified) |
|----------|-------------------|
| Pinned CLI version | `@openai/codex@0.142.5` (npm `latest` as of 2026-07-03) |
| Model ID | `openai.gpt-5.5` |
| Endpoint | `https://bedrock-mantle.us-east-1.api.aws/openai/v1` (Responses API only) |
| Auth mode (v1) | AWS SDK credential chain (IRSA SigV4) — **no bearer token, no Secrets Manager** |
| Bearer-token fallback | `AWS_BEARER_TOKEN_BEDROCK` supported but **not needed**; keep as break-glass |
| gpt-oss fallback | Not required. `gpt-oss-120b` works via `bedrock-runtime` but is only the fallback if mantle is blocked org-side (SCP) |
| SigV4 signing name (for #2709) | **`bedrock`** (service name), region `us-east-1`. Confirmed working |

**One open item** (does not block the GO): the successful SigV4 test ran under a role with
`AdministratorAccess`. The *scoped* agent-worker policy must be confirmed to authorize the
mantle path (see §6). Cheap to verify in #2704's smoke test.

---

## 2. Environment used for verification

| Item | Value |
|------|-------|
| Account | `879318057152` (dev) — **note:** research comment referenced `193832579677`; findings are account-independent |
| Region | `us-east-1` |
| Identity | `arn:aws:sts::879318057152:assumed-role/adp-dev-agent-scaledjob-role/...` (the IRSA/ScaledJob role) |
| Bearer token | `AWS_BEARER_TOKEN_BEDROCK` **unset** throughout (proves SDK-chain-only auth) |
| Node / npm | v24.18.0 / 11.16.0 |

This is the credential context a KEDA agent-worker pod runs under, so the auth result is
directly representative — not a human SSO session.

---

## 3. Model availability (confirms the research comments)

`list-foundation-models` shows only the `bedrock-runtime` OpenAI models — GPT-5.5 is
**not** there, and its absence is **not** an access problem:

```
$ aws bedrock list-foundation-models --region us-east-1 \
    --query "modelSummaries[?contains(modelId,'openai')].[modelId,modelLifecycle.status]"
openai.gpt-oss-120b-1:0        ACTIVE
openai.gpt-oss-20b-1:0         ACTIVE
openai.gpt-oss-safeguard-120b  ACTIVE
openai.gpt-oss-safeguard-20b   ACTIVE

$ aws bedrock get-foundation-model-availability --model-id openai.gpt-5.5 --region us-east-1
An error occurred (ValidationException) ... The provided model identifier is invalid.
```

GPT-5.5 is served **only** from `bedrock-mantle` (Responses API), so it will never appear
in `list-foundation-models` and `get-foundation-model-availability` rejects it. Matches the
model card cited in the issue comments. **Nothing to "enable."**

`gpt-oss-120b` (the fallback) does work on `bedrock-runtime` via Converse:

```
$ aws bedrock-runtime converse --region us-east-1 --model-id openai.gpt-oss-120b-1:0 \
    --messages '[{"role":"user","content":[{"text":"Reply with exactly the word: PONG"}]}]' \
    --inference-config '{"maxTokens":16,"temperature":0}'
# -> HTTP 200, stopReason max_tokens, latencyMs 533
```

---

## 4. Auth mode — SigV4 against mantle works with IRSA (the key result)

Unsigned request confirms the endpoint is reachable and requires auth:

```
$ curl -s https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses -X POST \
    -H "Content-Type: application/json" \
    -d '{"model":"openai.gpt-5.5","input":"ping","max_output_tokens":16}'
{"error":{"code":"invalid_api_key","message":"Missing 'authorization' or 'x-api-key' header",
  "type":"permission_denied_error"}}   # HTTP 401
```

Signing the **exact same body bytes** with botocore `SigV4Auth` using the pod's role
credentials succeeds. **Both** candidate signing names return HTTP 200:

```python
# service name "bedrock"        -> HTTP 200
# service name "bedrock-mantle" -> HTTP 200
# (region us-east-1, path /openai/v1/responses, IRSA creds, AWS_BEARER_TOKEN_BEDROCK unset)
```

Full invocation (representative, for latency/shape — Responses API):

```
POST https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses
body: {"model":"openai.gpt-5.5","input":"<code task>","max_output_tokens":256}
-> HTTP 200  latency 1.43s
   status: completed   model: openai.gpt-5.5
   usage: {"input_tokens":26,"output_tokens":81,
           "output_tokens_details":{"reasoning_tokens":71},"total_tokens":107}
   output text nested under output[].content[].output_text
```

**Answer for #2709:** mantle accepts SigV4. Use signing service **`bedrock`**, region
`us-east-1`. This makes gateway-passthrough **design option (a)** viable — the gateway can
re-sign with SigV4 instead of holding a bearer token (option b). SigV4 pitfalls to respect:
sign & send the *identical* body bytes, use the `openai/v1/responses` path, and use
Responses-API field names (`input`, `max_output_tokens`; output text under `output`).

---

## 5. Codex CLI wiring + headless verification

**Pin: `@openai/codex@0.142.5`.** The npm package is a ~16K launcher that fetches a
per-platform binary (`0.142.5-linux-x64`, etc. — pin the platform dist tag in the image).

**Critical finding — Codex 0.142.5 ships a *built-in* `amazon-bedrock` provider.** It
**rejects** custom `base_url`/`wire_api`; only `aws.profile` and `aws.region` are
overridable:

```
Error: model_providers.amazon-bedrock only supports changing `aws.profile` and
`aws.region`; other non-default provider fields are not supported
```

The built-in provider already points at mantle correctly (from trace logs):

```
provider = "Amazon Bedrock"
base_url = "https://bedrock-mantle.us-east-1.api.aws/openai/v1"
wire_api = Responses
http_headers = {"x-amzn-mantle-client-agent": "codex"}
aws = { region: "us-east-1" }   # SigV4 via SDK chain; no bearer token
```

### 5.1 Working `~/.codex/config.toml` (minimal — do NOT set base_url/wire_api)

```toml
model = "openai.gpt-5.5"
model_provider = "amazon-bedrock"

[model_providers.amazon-bedrock]
aws.region = "us-east-1"
```

### 5.2 Reproducible headless transcript (exit 0, no TTY, no secrets)

```
$ export CODEX_HOME=/some/writable/dir      # config.toml above lives here
$ AWS_REGION=us-east-1 codex exec \
    --dangerously-bypass-approvals-and-sandbox \
    --skip-git-repo-check \
    -o /tmp/codex-last.txt \
    "Write a Python function is_prime(n) ... Reply with only the code in a fenced block."
# EXIT: 0
# /tmp/codex-last.txt:
# ```python
# def is_prime(n):
#     if n <= 1: return False
#     ...
#     return True
# ```
# tokens used: 7,811
```

Confirmed for §Design task 3 (headless): `codex exec` reads the prompt as an arg (or stdin),
emits the final message to `-o <file>`, streams JSONL with `--json`, honors
`--skip-git-repo-check` outside a repo, and **never prompts interactively** when
`--dangerously-bypass-approvals-and-sandbox` is set (the intended KEDA-pod flag). Exit code
is a reliable success/fail signal.

### 5.3 KEDA-pod gotchas discovered

- **Plugin featured-cache call:** Codex makes a best-effort GET to
  `https://chatgpt.com/backend-api/plugins/featured` on startup; with no ChatGPT auth it
  logs `WARN ... 401 Unauthorized` and **continues** — non-fatal. But a pod with locked-down
  egress that *blocks* (rather than refuses) that host could stall on connect. Recommend
  allowing the mantle host explicitly and letting chatgpt.com fail fast, or pre-seeding an
  empty plugin cache. Verify egress behavior in #2704.
- **`CODEX_HOME` must be writable** (session snapshots, shell snapshots). Point it at a
  writable ephemeral dir in the pod; Codex refuses to create helper binaries under `/tmp`
  but still runs.
- Use `--dangerously-bypass-approvals-and-sandbox` (headless, externally-sandboxed pod) plus
  `--skip-git-repo-check` for non-repo working dirs.
- "Fast Mode" is unavailable on Bedrock (per OpenAI docs) — not used here.

---

## 6. IAM for the mantle path (open item for #2704)

The ScaledJob role's scoped inline policy (`agent-worker-scoped-permissions`) grants:

```json
{
  "Sid": "BedrockModelInvoke",
  "Effect": "Allow",
  "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
  "Resource": [
    "arn:aws:bedrock:*:*:inference-profile/*",
    "arn:aws:bedrock:*::foundation-model/*"
  ]
}
```

My successful SigV4 test ran under the role's **`AdministratorAccess`** attachment, which
masks whether mantle authorizes against the *scoped* action/resource above. **To confirm in
#2704:** run the same headless `codex exec` under only `agent-worker-scoped-permissions`. If
it returns `AccessDeniedException`, the likely fixes (in order of probability) are:

1. Mantle may authorize against `bedrock:InvokeModel` on a `foundation-model/openai.gpt-5.5`
   ARN — already covered by the `foundation-model/*` resource → should pass.
2. If mantle uses a distinct action (e.g. a `bedrock:*Responses*` verb) or a mantle-specific
   resource ARN, extend the `BedrockModelInvoke` statement's `Action`/`Resource` accordingly.

First successful invoke auto-subscribes the model (docs note a ~15-min settling window;
transient `AccessDeniedException` during it is expected — retry).

---

## 7. Handoff to stories 2–4 (#2704+)

- **Config + auth are settled:** pin `0.142.5`, use the minimal `config.toml` in §5.1, rely
  on IRSA SigV4 — no Secrets Manager entry needed for v1.
- **#2709 (gateway passthrough):** mantle accepts SigV4 (`bedrock`/`us-east-1`) → design
  option (a) is viable; bearer-token (option b) is the fallback only.
- **#2704 smoke test must:** (1) re-run the §5.2 transcript under the scoped role (not admin)
  to close the §6 IAM item; (2) confirm pod egress allows the mantle host and tolerates the
  chatgpt.com plugin-cache 401.
- **Fallback ladder if mantle is blocked org-side (SCP):** `openai.gpt-oss-120b-1:0` on
  `bedrock-runtime` (Converse/InvokeModel) — verified working, but note Codex's built-in
  `amazon-bedrock` provider targets mantle, so an oss fallback needs a *custom* provider
  entry pointing at `bedrock-runtime` (different wire_api) — out of scope for this spike.
