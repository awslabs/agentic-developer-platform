# Independent job-host reconciliation

The reconciler uses the scheduled S3 entry point
`python3 scripts/reconcile_instances.py --environment dev --region <approved-region> --group-ids <approved-numeric-list>`.
It requires the dedicated `adp-dev-gitlab-runners-reconciler` GitHub environment
role, not a platform deployment role. The operator supplies the protected
account, region and approved group list. Terraform creates that role, one
non-secret String heartbeat parameter per configured group, and an exact
manager role permission to update only its own parameter. These are runner-state
resources; destroying runner state removes the parameters and role.

The guard sidecar writes its heartbeat before acknowledging a manager start.
The manager process then pulses a memory-backed file every ten seconds; the
sidecar renews its AWS heartbeat only while that file stays fresh. If AWS
renewal stops, the manager's watchdog ends the runner before the scheduled
reconciler acts. Reconciliation reads the server's parameter modification
time rather than trusting the parameter value as a timestamp. An age of more
than 120 seconds (or an inactive initial parameter) makes a group eligible.
Healthy managers and their assigned instances are left alone.

Every requested ASG must have the exact environment and group tags, and EC2
instances must match its group identity and AWS Auto Scaling owner tag.
After liveness expires, the reconciler removes ASG scale-in protection,
sets desired capacity to zero, and terminates only detached instances that
retain the verifiable owner tag. It waits up to six minutes and exits with a
failure if **any** owned active instance or ASG member remains. Missing,
ambiguous or mismatched inventories fail without attempting an unrelated
termination; operators must investigate those failures rather than relaxing
owner checks. The manager deployment and shared GitLab resources are never
deleted by this command.

With a five-minute schedule, at most 30 seconds of pulse freshness, a
120-second heartbeat window and a six-minute verification window, the
nominal bound is 13.5 minutes plus dispatch/API overhead. The workflow's
420-second timeout covers API overhead, but real schedule latency, AWS
termination and detached-tag behavior require #9291 live qualification;
this repository does not claim a live fifteen-minute measurement.
