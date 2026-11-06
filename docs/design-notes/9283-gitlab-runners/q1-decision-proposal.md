# Q1 executor decision — resolved by owner amendment

The owner approved temporary EC2 job instances on 2026-11-01.
[The current enforcement contract](q1-enforcement-contract.md) selects Docker
Autoscaler with AWS Fleeting, managers on EKS and disposable private EC2 jobs in
the same VPC. It replaces the earlier Custom executor proposal and preserves
S1, tenant isolation, strict TLS, resource bounds and live acceptance gates.
Implementation and qualification remain pending; this decision is not a live
security verdict. Historical proposals remain in Git history.
