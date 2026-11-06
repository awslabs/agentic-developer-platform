# Dev Box SSH: operator audit and acceptance

This feature allows an authenticated **owner** to request temporary AWS
credentials narrowed by an STS session policy to the recorded current instance
and the `AWS-StartSSHSession` document. EC2 Instance Connect accepts an
`ec2-user` public key for roughly 60 seconds. The guest listens only on
127.0.0.1:22; there is no public IP or security-group rule for port 22.

## Deployment prerequisites

1. Build and verify the approved private base image with
   `image/prepare_ssh.sh`. The build refuses an image missing OpenSSH, EC2
   Instance Connect's authorized-key helper, SSM agent, or loopback-only SSH
   settings. The derived Crew image must inherit that base; the source revision
   and AMI must be approved through the normal Dev Box stage. This PR's
   local tests do **not** prove an AMI was built.
2. Supply the **exact gateway IAM role ARN** for this deployment as Terraform
   `gateway_principal_arn`. Apply the additive Dev Box module, confirm its
   `ssh_grant_role_arn` output belongs to the same account, and provide that
   output as gateway environment variable `BG_DEV_BOX_SSH_GRANT_ROLE_ARN`.
   When no gateway role is specified, Terraform creates no grant role; when
   the gateway variable is unset, the SSH API cannot issue a grant. Do not
   substitute the controller, guest or pod workload role.
3. Verify the selected AWS account **and exact assumed operator role** through
   the approved credential connection before any AWS operation. Confirm the
   Dev Box SG has **no inbound SSH**. With dashboard disabled it has no ingress;
   if dashboard is enabled, its only intentional inbound exception is gateway
   security group to port 8443. Never add inbound 22, public IP or SSH keys to
   guest user data.
4. Agree the **account-wide Session Manager session preferences** with the
   platform operator before claiming a maximum lifetime for an already-open
   SSH tunnel. Set an approved absolute maximum session duration (for example,
   60 minutes) and idle preference through the authorized Session Manager
   administration process; these settings can affect other Session Manager
   users in that account. The image separately sets interactive Bash idle
   timeout to 900 seconds, closes disconnected SSH clients after keepalive
   failures and permits at most eight SSH sessions per connection. STS expiry
   alone does **not** end an active tunnel. This account-wide setting is a
   rollout prerequisite, not a Terraform test result.

## Audit without capturing secrets

- The gateway writes a `ssh_access` operation and `ssh_access_issued` event
  with the box, owner, deployment and machine generation. It stores no AWS
  access key, SSH public/private key or session token. A refused non-owner
  request must not generate a success event or call STS.
- CloudTrail records `AssumeRole`, `SendSSHPublicKey`, and `StartSession` in the
  selected account. Correlate their timestamps, scoped role and instance ID
  with the owner-scoped gateway operation. Inspect the actual STS session
  policy to ensure it names **only** that instance and the AWS SSH document.
  The instance's EC2 owner/deployment tags and the gateway's machine generation
  must match the current Dev Box record.
- Session Manager session history provides start/termination metadata.
  **SSH traffic inside Session Manager is encrypted end to end: Session Manager
  logging does not record commands or shell output for SSH/port-forwarded
  sessions.** Do not claim an SSH command transcript or successful login from
  CloudTrail or a Session Manager logging destination alone. Ask the owner for
  a consenting live shell/IDE/Crew acceptance result; do not capture credentials
  or private home-directory contents in evidence.

## Operator acceptance after merge and dev deployment

1. On a real owner laptop with the required tools and verified server host
   fingerprint, run the local helper from the repository root:

   ```bash
   BOX_NAME=assistant modules/user-services/dev-box/tests/live_ssh_acceptance.sh
   ```

   Replace `assistant` with the actual owned box name or ID. This command does
   not provision anything. It verifies the alias and remote `ec2-user` and then
   opens `adp devbox ssh` for an interactive shell. Exit that shell after
   confirming the expected host.
2. Check CloudTrail for a `StartSession` on **that box's instance only**, the
   key-push and STS grant, and correlate the gateway issuance operation.
   Recheck the guest SG in the named dev deployment: no port-22 ingress.
3. Using the written alias, connect in VS Code Remote - SSH, Kiro IDE Remote
   SSH and Kiro Crew remote-host SSH mode; run a harmless remote command in
   each and confirm the correct box. A desktop or shell accepting the host
   alias is not proof that Crew actually ran remotely.
4. As a different authenticated owner, request the first box: expect 404/403,
   **no** STS grant and **no** successful gateway event. Do not record tokens
   or full API responses as public evidence. Retest after box replacement
   to ensure the old generation no longer receives new grants.

These live checks belong to the operator after merge. The ready implementation
PR is not evidence of deployment or accepted live SSH.
