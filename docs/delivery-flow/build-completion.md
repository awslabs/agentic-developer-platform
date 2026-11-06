# Build completion in a mixed delivery flow

A flow may authorize deployment while some of its stories only produce reviewed
code. Set `delivery_mode: "code_only"` on those develop nodes, with an explicit
agent executor and `evidence_expectations: ["verified-merge"]`. The engine still
requires review and an independently verified merge before concluding them.
Keep deployment, live verification and evidence acceptance in their own dependent
nodes. The mode grants no deployment or evaluation authority.

Omitting this field retains the existing behavior: the flow policy determines
whether a code story continues into deployment. The field cannot be set on an
operate node, evaluation or gate. An amendment changing it changes that node's
execution contract; it does not preserve an in-flight assignment automatically.

For an existing build that has already passed its verified merge but is held in
`deployment_pending`, the plan approver can use the bounded recovery API:

1. Pause new flow effects with `POST /orchestration/flows/{flow_id}/execution`.
2. Preview `POST /orchestration/flows/{flow_id}/build-completion/preview` with
   `node_id`, `deployment_node_id`, `acceptance_node_id`, the current
   `expected_plan_version` and `expected_plan_hash`, and a reason.
3. Review the exact merge receipt and retained downstream nodes. Submit the same
   body plus the returned `expected_snapshot` to the sibling `/accept` endpoint.
4. Allow the engine to reconcile the verified build, then resume the flow.

Recovery requires a separate operations node and an acceptance evaluation reachable
in both the accepted plan and actual graph. Acceptance may be human, or a machine
repository evaluation that reads workflow artifacts bound to the retained
operations result and verifies their workflow provenance. Code checks alone do
not qualify as machine delivery acceptance. The original evaluation definition
and predicates remain part of the recovery receipt.

A legacy develop node with empty criteria and evidence metadata may use this
explicit recovery after verified merge; its accepted definition is preserved.
The absence of metadata does not make a node code-only automatically, and recovery
cannot discard additional build evidence or criteria. Fresh code-only declarations
still require `evidence_expectations: ["verified-merge"]`.

Recovery refuses an unverified merge, pending
effect, recorded deployment effect, changed snapshot, foreign tenant or service
session. A contended execution returns a retryable conflict. Acceptance records
an attributed boundary decision and fences stale observations; it does not mark
deployment verified or approve the downstream evaluation. It preserves the accepted plan,
claims, attempts, budget meter and evidence history. A lost response can be
retried without accepting twice.

No migration or new workflow is required. Ship the gateway code through the
existing gateway release workflow. Local verification uses the PostgreSQL merge,
recovery, deployment-completion and plan-lineage suites. Qualify the recovery API
against the installed gateway before applying it to a live flow.
