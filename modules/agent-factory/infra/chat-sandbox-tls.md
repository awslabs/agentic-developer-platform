# Restricted sandbox HTTPS transport

The dedicated gateway listener terminates TLS inside a trusted gateway process.
It does not proxy capabilities over a plaintext network hop. It serves only an
explicit sandbox route allowlist, through the ordinary gateway application and
its workload/capability, owner, lease, feature-gate and budget checks. Internal
supervisor admission and exit routes are deliberately unavailable on this port.
The ordinary gateway entrypoint and public ingress do not start or expose it.

Endpoint: `https://chat-sandbox-gateway.adp-gateway.svc:8443`.
The endpoint is a ClusterIP Service selecting only the separate
`app.kubernetes.io/name=chat-sandbox-gateway` deployment in `adp-gateway`.

## Source and installation boundary

This component supplies the TLS listener, sandbox launch binding and an offline manifest renderer. It
does **not** activate chat, install admission/network policies, create a signing
authority, or claim a completed sandbox turn. The sandbox launch
preflight refuses missing or inconsistent trust, Service, admission and network
controls. Source checks do not establish installed enforcement; #6937 owns that
qualification before activation.

1. Obtain a dedicated server leaf certificate and key from an operator-managed
   private issuer. Its only DNS SAN is the endpoint hostname above. The leaf
   must have `CA=false` and `serverAuth`; the issuer has `CA=true` and
   `keyCertSign`. Both must remain valid for at least 48 hours. The renderer
   accepts a direct leaf/root chain and verifies the signatures, validity,
   hostname and matching key. Never place the issuer's private key in either
   namespace. Certificate renewal is an owned deployment operation.
2. Build the reviewed gateway source containing `src.agentauth.chat_tls` and
   the complete delegated runtime. Capture the current gateway Deployment as
   JSON and verify its immutable image digest belongs to that reviewed source.
   Render with `platform/scripts/render-chat-tls.py --deployment-json FILE
   --certificate LEAF.pem --private-key KEY.pem --ca ROOT.pem --output FILE`.
   Output is created exclusively with mode0600, contains the server private
   key, and must not be committed, logged, or published as a CI artifact.
3. Review the separate Deployment, ClusterIP Service, TLS Secret, immutable
   public-CA ConfigMap, named-ConfigMap read Role/RoleBinding and destination ingress policy. Existing gateway
   resources are not rewritten. The new process retains the observed gateway
   service account, application configuration, feature gates and image digest;
   it has the same trusted gateway role, not a sandbox role. It has no public
   Ingress. Its source/digest must participate in subsequent gateway releases.
4. The sandbox template mounts only the public CA ConfigMap read-only and sets
   `NODE_EXTRA_CA_CERTS` to `/var/run/adp-chat-ca/ca.crt`. Supply the rendered
   ConfigMap name as `SANDBOX_CA_CONFIGMAP` when rendering the suspended
   supervisor Job; it becomes `ADP_CHAT_SANDBOX_CA_CONFIGMAP` in the supervisor.
   The ordinary gateway serving supervisor admission/exit also needs
   `ADP_CHAT_SANDBOX_CA_CONFIGMAP` set to that same name and `ADP_CHAT_DATA_URL`
   set to the sandbox endpoint above for its workload checks. These are public
   configuration values, not credentials. Keep the supervisor's own
   `GATEWAY_HTTPS_ORIGIN` pointed at the ordinary internal gateway, not this
   restricted listener. The TLS renderer sets the two workload-check values
   in its new gateway Deployment but does not modify the ordinary gateway.
   Launch preflight validates `immutable=true`, the certificate hash, validity,
   CA identity and exact public key name. Never mount
   the TLS Secret or issuer private key in a sandbox, or disable certificate
   verification. CEL admission, the template fingerprint, returned-pod comparison
   and gateway workload verification enforce the restricted fields.
5. Keep sandbox DNS egress denied. The trusted supervisor reads only the
   named Service in `adp-gateway`, bind its current private IPv4 ClusterIP to the fixed
   hostname through the sandbox's `hostAliases`, and verify the returned pod
   has that exact mapping. Give the supervisor only named-Service read access
   for this lookup. This is implemented by `resolveGateway`; preflight rechecks
   the binding before pod creation and refuses Service replacement between reads.
   Admission permits only the fixed hostname and a private IPv4 address; the
   trusted launcher and returned-pod comparison bind the exact Service address.
   No worker-provided destination IP is accepted. The sandbox uses the hostname for TLS
   verification even though it needs no DNS network access.
6. Keep the baseline ingress/egress deny policy. Add exactly one sandbox
   checked-in `chat-sandbox-gateway-egress.yaml` policy to namespace `adp-gateway` AND pod selector
   `app.kubernetes.io/name=chat-sandbox-gateway`, TCP8443. Update launch preflight to validate this
   exact allowed policy and reject all other effective ingress/egress grants.
   Do not allow arbitrary HTTPS, DNS, gateway HTTP, metadata, Kubernetes or
   AWS/provider destinations. Verify installed AWS CNI policy enforcement;
   source manifests alone do not establish it.
7. Before admission, verify the dedicated deployment is ready on the reviewed
   digest, its Service selects those pods, the leaf matches the mounted CA and
   hostname, and both admission and network controls match the reviewed source.
   Extend the listener's exact route list for new scoped completion routes
   only after their independent authentication is reviewed. A route allowlist
   never substitutes for workload and owner authorization.
8. Run actual sandbox tests: successful delegated owner turn, untrusted CA and
   wrong-host refusal, admin/internal/direct-provider/metadata/API-server and
   DNS denial, foreign pod/capability refusal, teardown and credential canary
   absence. Local TLS tests prove only listener transport behavior.

For rotation, distribute new public trust to the trusted template before
changing the leaf, or drain existing bounded turns before replacing the CA and
leaf together. Never silently overwrite the immutable CA ConfigMap; its name
changes with the PEM content. Retain old trust only as long as verified active
turns need it, then clean up run-owned resources through the normal release
procedure. Readiness HTTPS probes do not verify certificate trust; the explicit
client handshake and live sandbox checks above are required independently.
