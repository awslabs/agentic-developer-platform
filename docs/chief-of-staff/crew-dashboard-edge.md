# Crew dashboard edge (issues #7205, #7902) and first-party handoff

Crew uses a dedicated HTTPS origin rather than a path below the ADP frontend.
The gateway infrastructure offers two shapes for that origin, selected with
`crew_dashboard_edge` once `crew_dashboard_enabled = true`; both write
`/adp/<env>/gateway/crew-dashboard-origin`, which the deployment renders into the
gateway's `CREW_DASHBOARD_ORIGIN` (Deployment env and the shared ConfigMap, because the
Dev Box controller reads the origin from the ConfigMap to configure each host's Kiro Crew
`dashboard.url`), and both reach the same gateway service, whose
Crew proxy selects the dashboard by the request's `Host` header.

| Mode | `crew_dashboard_edge` | Needs | Edge resources | Idle streams |
|---|---|---|---|---|
| ALB (#7205) | `"alb"` (default) | public Route 53 zone, hostname, frontend custom domain in the same zone (still enforced by a plan-time precondition; the handoff below no longer depends on it, so relaxing that check is a follow-up) | public ALB (own ingress class), DNS-validated ACM certificate, Route 53 alias | 300 s ALB idle timeout |
| CloudFront (#7902) | `"cloudfront"` | nothing public: reuses the frontend's VPC origin to the internal gateway ALB | one distribution on its own `*.cloudfront.net` name | 60 s origin read timeout, kept alive by the proxy's 25 s keepalive |

The existing CloudFront frontend and `/api` routes remain separate in both modes.

## ALB mode

The gateway infrastructure creates one public, host-scoped ALB ingress to the
existing gateway service, a DNS-validated ACM certificate and a Route 53 alias.
The ingress class preserves the viewer's `Host` header and sets a 300-second ALB
idle timeout; the ALB does not cache responses and supports WebSocket upgrades.
This mode remains the choice for accounts that own a public zone and want no
CloudFront hop in the Crew path.

## CloudFront mode

`crew_dashboard_edge = "cloudfront"` creates a dedicated distribution
(`modules/gateway/infra/crew-dashboard-cloudfront.tf`) and none of the ALB-mode
resources: no certificate, no DNS record, no public load balancer, no ingress
class. The distribution uses the frontend's existing VPC origin to the internal
gateway ALB with `Managed-CachingDisabled` and `Managed-AllViewer` (the viewer's
`Host`, cookies, query string and `Upgrade` headers are forwarded, exactly as the
frontend's `/api/*` behaviour already does on the same origin), all HTTP
methods, `https-only` viewers, HTTP/2, no compression, the frontend's price
class, and the frontend's WAF web ACL when one is configured. The SSM origin
becomes `https://<distribution-id>.cloudfront.net`.

Routing assumption: the gateway Ingress (`modules/gateway/k8s/ingress.yaml`)
has path-only rules, so the shared internal ALB forwards every `Host` to
`bedrockgateway:80` on `/`; `/tools*`, `/agent/tools*`, `/api/tools*` and
`/api/agent/tools*` go to `tools-facade` for every Host and are not dashboard
paths. No new ingress is needed.

Preconditions enforced at plan time:

- `crew_dashboard_hostname` and `crew_dashboard_zone_id` must be empty (use
  ALB mode for a hostname).
- `enable_vpc_origin`, `internal_alb_arn` and `internal_alb_dns` must be set,
  which is the ALB second pass (`platform/scripts/wire-gateway-alb.sh` or
  `gateway-infra-apply.yml`). Flip the mode on only on an account where that
  pass has already run.

### Trade-off: 60-second origin read timeout

CloudFront caps a VPC origin's read timeout at 60 seconds. A Crew SSE or
WebSocket stream that stays silent longer than that is closed by the edge and
the dashboard reconnects. The Crew proxy therefore keeps bytes moving while a
stream is idle (`modules/gateway/src/crew/keepalive.py`):

- SSE and other `text/event-stream` bodies receive the comment line
  `: keepalive\n\n` after each idle interval; EventSource clients ignore
  comments, and data chunks are never reordered or split.
- The WebSocket hop to the Crew pod pings at the same interval; the browser hop
  is pinged by the ASGI server itself (uvicorn `ws_ping_interval`, default
  20 s, settable with `UVICORN_WS_PING_INTERVAL`), because ASGI has no ping
  event. Pings are control frames, so message order is untouched.

`CREW_PROXY_KEEPALIVE_SECONDS` sets the interval (default 25, `0` disables).
Keep it below 60 in CloudFront mode; ALB mode also benefits from it against
intermediaries with shorter idle limits. The infrastructure publishes the value
as `/adp/<env>/gateway/crew-proxy-keepalive-seconds` (`crew_proxy_keepalive_seconds`),
and both gateway renderers (`platform/scripts/deploy-all.sh` Step 4,
`.github/workflows/gateway-deploy.yml`) read it on every deploy into the
ConfigMap's `CREW_PROXY_KEEPALIVE_SECONDS` and the Deployment's
`UVICORN_WS_PING_INTERVAL`; at `0` the uvicorn variable is removed from the
manifest rather than set to `0`, which uvicorn would treat as a tight ping loop.
`crew-proxy-max-conn-per-user` (`CREW_PROXY_MAX_CONN_PER_USER`, default 64 — the Kiro Crew SPA fetches ~28 chunks concurrently on first load) and
the checkpoint parameters from `crew-checkpoints.tf` (`ADP_CREW_CHECKPOINTS_ENABLED`,
`ADP_CREW_CHECKPOINT_BUCKET`, `ADP_CREW_CHECKPOINT_RETENTION_SECONDS`) are rendered
the same way, so flipping `crew_dashboard_enabled` / `crew_checkpoints_enabled` and
re-running the deploy is all an operator does.

### No request-rate admission at the proxy

The Crew proxy applies **no per-user request-rate limit** of its own. The connection cap
above is the only proxy-level bound, and it covers static assets, the dashboard's API
calls, SSE streams and WebSocket upgrades alike. Model traffic from the box is limited
where it happens — on the gateway's model route, under the owner's identity — so
debiting the dashboard's requests from that same bucket only double-counted them: with
the default 60 rpm (a 15-token burst) the Kiro Crew SPA's first load requested ~213
chunks and 188 were refused with 429 (#8223 cause 24, dev 2026-10-20). Cache validators
(`ETag`, `Cache-Control`) pass through unchanged, so a reload is a run of 304s.

## Handoff: how the browser acquires the Crew session cookie

The Crew host (`modules/gateway/src/crew/dashboard.py`) admits a request only
when it carries the `adp_crew` cookie: a signed binding of the ADP identity to
the exact active lease (target kind, reference, generation), `Secure`,
`HttpOnly`, `SameSite=Lax`, host-only. Setting that cookie from the ADP frontend
is the hard part when the two origins are **cross-site**. `cloudfront.net` is a
public suffix, so a CloudFront Crew edge is cross-site to every frontend,
including another `*.cloudfront.net` name, and browsers drop a `SameSite=Lax`
cookie delivered by a cross-site fetch response. An earlier version of the
frontend did exactly that (a CORS `fetch` to `/crew/handoff`) and would have
seen 409 `crew_handoff_required` on the following navigation; ALB mode only
avoided it by requiring the frontend to share the Crew zone.

The handoff therefore runs as two first-party steps, in both edge modes:

1. **Ticket (ADP origin, same-site to the frontend).** The frontend
   (`frontend/src/services/crewDashboard.ts`) calls
   `POST /crew/handoff-ticket` on the ADP API with its bearer token. The gateway
   (`src/crew/proxy.py`) checks chat membership and the active lease and returns
   `{"handoff_url": "https://<crew-origin>/crew/handoff?ticket=<ticket>", "expires_in": 60}`.
   The ticket (`src/crew/handoff_tickets.py`) is an HMAC-signed claim set bound
   to tenant, user, target kind, target reference and lease generation, with a
   random nonce and a 60-second lifetime. The frontend accepts the URL only if it
   is on the configured Crew origin, has path `/crew/handoff` and exactly one
   `ticket` parameter; nothing is ever fetched from the Crew origin by the page.
2. **Redemption (Crew origin, top-level navigation).** The frontend navigates
   to that URL with `location.replace` from the tab it opened synchronously on the
   owner's click (the `/settings/dev-box/open` interstitial, `pages/settings/DevBoxOpen`;
   the ticket is requested from that tab, so no late pop-up is ever opened and nothing
   blocks it). The Crew host verifies
   the signature and expiry, refuses any second query parameter, burns the nonce
   in Redis (`SET NX`, keyed by tenant, user and nonce), re-checks membership and
   that the lease is still active and identical to the ticket's claims, mints the
   Kiro Crew login link through the custody listener as before, writes the
   `crew_dashboard_handoff` audit row, and answers `302` to
   `https://<crew-origin>/?token=<link>` with `Set-Cookie: adp_crew=…`. Because
   the request is a top-level navigation, the cookie is first-party even when the
   frontend is cross-site. The redirect is same-site to the Crew origin, so the
   `Lax` cookie accompanies it and every later dashboard request and WebSocket
   upgrade. (`SameSite=Strict` would not: the redirected request still belongs to
   a navigation initiated from the ADP origin.)

Refusals are all `4xx` with `Cache-Control: no-store` and never set a cookie:
`401 crew_handoff_refused` for a malformed, tampered, expired, replayed or
duplicated ticket; `404` when the ticket's owner has no Crew or is not a chat
member; `409 lease_fenced` when the lease has moved on or the ticket names a
different target; `503 crew_handoff_unavailable` when the single-use registry is
unreachable (fail closed). A ticket is accepted only on `/crew/handoff`; it is
never an identity for dashboard paths or the WebSocket. The bearer variant of
`/crew/handoff` (JSON `redirect_url` for `Accept: application/json`, `302`
otherwise) stays for same-site callers and older frontends.

The ticket travels in the query string of one navigation, so the gateway's
request loggers redact `ticket` (and the Kiro `token`) to `[redacted]`
(`src/shared/middleware/logging_middleware.py`, `src/admin/middleware.py`);
the redirect carries `Referrer-Policy: no-referrer`. Residual exposure: the
ASGI server's own access log (uvicorn, enabled by the image CMD) prints request
lines including query strings. A logged ticket is single-use and already burned
by the time it is written, and it expires within 60 seconds, but operators who
ship container stdout to a shared sink should treat that log as sensitive, as
they already must for the existing `/?token=` line.

`CREW_DASHBOARD_ORIGIN` is also the only accepted WebSocket `Origin`
(`src/crew/websocket.py`), so in CloudFront mode the upgrade from the dashboard
page on `https://<distribution-id>.cloudfront.net` is accepted and one from the
frontend distribution, or any other site, is refused with `403`.

## Activation and recovery

`crew_dashboard_enabled` defaults to `false`: no Crew certificate, DNS record,
public ALB, ingress class, distribution or origin parameter is created while it
is off. In ALB mode the operator supplies `crew_dashboard_hostname` (for
example, `crew.team.example.com`) and `crew_dashboard_zone_id` for its public
Route 53 zone through private deployment configuration; the gateway and ACM
certificate must be in us-east-1. Do not put a real hostname or zone ID in this
repository. In CloudFront mode the operator sets `crew_dashboard_edge =
"cloudfront"` and leaves both empty; `environments/dev/modules/gateway.tfvars`
carries the two lines commented out.

**Do not turn the flag on until the gateway's Crew proxy (#7206) authenticates
and authorizes every path on this origin.** Both edges target the shared gateway
service; enabling either without a host-level authorization gate could expose
existing gateway routes. ALB mode adds a public ALB beyond the issue's one-rule
cost assumption; CloudFront mode adds one distribution. The operator must review
that cost and the public ingress before activating either.

Use the existing `gateway-infra-apply.yml` deployment path after those gates
are met. For recovery, set `crew_dashboard_enabled=false` through the same
private configuration and apply; confirm the ALB, certificate, DNS alias or
distribution, and the parameter have been removed. Do not change the frontend
domain or its CloudFront distribution.

## Post-deployment checks (operator)

Replace `<crew-origin>` with the privately confirmed origin after both this
infrastructure and the authenticated proxy are deployed. Record results in
private issue evidence, not public documentation.

```bash
curl -i 'https://<crew-origin>/api/health'
curl -i 'http://<crew-origin>/'
```

The first response must be Crew-proxy JSON (401 without a session or 200 with
a session), never the frontend HTML. The second must redirect to HTTPS (301 or
308) in ALB mode or be refused (403) in CloudFront mode, which serves HTTPS
viewers only.

Idle-session acceptance (both modes, required in CloudFront mode): open a Crew
dashboard through the edge, start a live view that holds an SSE or WebSocket
connection, and leave the browser idle for 15 minutes. In the browser's network
panel the same connection must remain open for the whole period with zero
reconnects, and a message sent afterwards must arrive on that connection. Run
the existing frontend and `/api` smoke checks from `deploy-frontend.sh` as a
separate compatibility check. Static Terraform checks alone do not establish
any of these live results.

Per-user wildcard hosts are **not** provisioned. If that option is selected
later, it needs a separate DNS/certificate and identity-routing design;
changing the shared origin to a wildcard is not part of this rollout.

## Crew pod handoff contract

The chat-agent rollout may set `ADP_CREW_DASHBOARD_ORIGIN` to a validated HTTPS
origin such as `https://crew.example.test`. When absent, Kiro Crew stays
loopback-only and its custody listener does not start. With the origin set, the
pod binds Kiro Crew to all pod interfaces, renders that origin into the dashboard
configuration and CORS settings, and listens for `POST /mint` on port 5477.
The bind is `0.0.0.0` rather than the proposed pod IP because the pinned Kiro
Crew mint CLI must connect to `127.0.0.1:5476`, and Kiro Crew supports only one
bind address. Kubernetes NetworkPolicy supplies the network boundary; the
custody listener itself still binds only the pod IP.
The `crew-pod-dashboard-ingress` policy allows only `bedrockgateway` pods in
`adp-gateway` to reach ports 5476 and 5477; the existing crew deny-all remains.

The gateway must send a JSON body with `run_id`, `lease_generation`, `user_id`
and a unique UUID `request_id`, plus `X-Adp-Crew-Signature`: a gateway control
envelope with audience `adp-crew-dashboard-mint`, action `dashboard.mint`,
command ID equal to `request_id`, target run/generation equal to the body's
lease identity, principal equal to `user_id`, and body digest of the *exact*
JSON bytes transmitted. The envelope expires within 30 seconds. The pod loads
verification-only keys over its projected-workload-token-authenticated gateway
connection, checks the current signed model decision's owner and generation,
and returns only the link token, its expiry and lease generation. Requests are
single-use; no Kiro Crew login credentials leave the pod. Gateway proxy work
must add `Secure` to forwarded `Set-Cookie` because Kiro Crew only honors
`X-Forwarded-Proto` from loopback. Kiro Crew treats proxied sessions as remote,
so secret reveal stays read-only.
