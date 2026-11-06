# Operations recovery stays with the agent

The operations agent resolves repair checks, merges its qualified repair, triggers
the accepted GitHub Actions workflow, and gathers deployment and runtime evidence.
The normal bounded worker retry path handles an exited worker. Scheduled execution
no longer transfers failed operations stories into a deployment controller.

Existing controller assignments retain their reconciliation handler until they
finish or an operator closes them. The operator control at
`POST /orchestration/nodes/{node_id}/operations-agent-recovery/preview` and `/accept`
closes a legacy assignment whose only unresolved effect is an unknown deployment
dispatch. It requires a paused flow, the exact execution, attempt and original
report digest, and an authenticated platform administrator's evidence that the
provider was never called. A fresh GitHub read must also find no correlated run.
Absence of a workflow alone does not establish that dispatch was never attempted;
the operator must inspect the original failure before attesting reconciliation.

Acceptance is bound to the preview snapshot. The original failed report, transfer,
action intent, prior unknown observation, policy and attempt count remain in the
journal. The action is settled as failed, the execution is concluded and its claim
released. The node returns to failed, with the flow still paused. This operation
does not create a worker, reset attempts, increase limits, mark acceptance, merge
code or dispatch a workflow. Existing retry-limit and node-resume controls govern
the next worker, which must pass ordinary admission and spending checks.

An optional maintenance source can preserve the agent/engine correction already
deployed to the same target. It must contain the original release and qualified
repair, lie on the accepted default branch, have a successful run of the same
deployment workflow, and leave that workflow's definition unchanged. Its receipt
is retained separately from the native repair receipts. The next operations agent
deploys and verifies that revision; maintenance evidence does not pass the story.
Later repair merges extend this recorded source normally. Verified limit increases
can preserve these receipts, while unrelated policy changes cannot.
