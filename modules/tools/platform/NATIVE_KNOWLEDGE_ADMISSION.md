# Native Knowledge read admission source

This draft source extends the native admission implementation in PR #8166 and
the private Knowledge mediator in PR #8174. It is prepared on their explicit
integration base. It does not install a deployment binding, fixture, consent,
price contract, credential, environment secret, or release.

The optional schema `3.0` native tool binding pins the existing private
Knowledge gateway/target configuration and the current dynamic schema digest.
Its separate coverage digest includes the mediator route. The schema `2.0`
Search composition remains separate. Missing or changed configuration refuses
native Knowledge enrollment and dispatch.

Only code search, understand, impact, browse (`ls`, `tree`, `read`, `info`) and
secure (`identify`, `plan`, `verify`) are in this read scope. Protected contracts
bind every original argument. Each read owns its original execution. Semantic
search, remember and experience retain their existing admission refusal.

The facade consumes the original native reservation before signing a request.
Native operation metadata is signed separately from Door arguments. The
mediator validates the original native approval, designation, current human
membership/consent, source contract, exact request/execution/owner binding and
retained hierarchy holds. It then conditionally updates that same journal
operation before its single Door request. A lost claim acknowledgement, lost
response, expired hold, cleanup race or replay cannot submit a second request.
The Door transport refuses redirects and bounds its request and response to
the protected contract's 64 KiB and 256 KiB limits.

Native discovery is separately admitted. Native calls do not implicitly spend
the call reservation on another discovery. The public schema comes from the
pinned, fresh private catalogue. A claimed native header without an actual
native admission refuses before provider dispatch.

Completed stateless reads can close only when their tracked accepted operations
are all resolved. Cleanup uses the original native execution and retained
reservation. It does not call Door again. Native status keeps funding,
functional cleanup and billing settlement separate. No hold is released by
this change, and no response digest establishes complete billed quantities.

## Remaining live prerequisites

- The draft #8166 runtime must be independently reviewed and adopted. This
  source PR and its integration base are not release authorization.
- A protected Knowledge registration compiler/owner input path must supply
  complete real source/price/enforced-quantity contracts for the schema `3.0`
  binding. The current Search registrar is not a Knowledge registrar.
- Installed native client transport and live runner/evidence integration are
  separate work. The existing AC04 live guard is unchanged by this source slice.
- AC04's positive controls require two distinct authentic owner/tenant
  sessions. A native capability authorizes only its original designated
  human/tenant and accumulator. Per-cell prices cannot be reported as an
  aggregate reservation across those owners. A shared cross-owner budget
  contract is not implemented here.
- The original worker authority path is unchanged. A human native capability
  does not establish the worker ACL control's run authority.
- The journal proves a mediator claim and, when recorded, receipt of an exact
  Door response. It is not an independent backend invocation counter, an
  authoritative index snapshot, proof that zero backend calls occurred, or
  complete provider billing evidence. Those AC04 oracle requirements remain.

Offline tests use real original-operation Dynamo/Redis admission and synthetic
deployment/provider fixtures. They do not claim live fixture, deployment,
index, billing, or acceptance success.
