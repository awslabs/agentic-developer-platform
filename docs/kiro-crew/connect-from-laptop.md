# Connect from your laptop to an ADP Dev Box

The box has no public IP or inbound SSH rule. ADP signs in the owner, then
issues a temporary grant for **that owner's current running instance**. Your
laptop sends a public key valid for about 60 seconds using EC2 Instance Connect;
Session Manager carries SSH to the guest's loopback port 22. You need no AWS
account or long-lived cloud credentials. The matching private key stays in your
own `~/.ssh/` directory; it is never stored on the box or sent to ADP.

## Before connecting

- Sign in on the laptop with `adp login`, select the intended deployment and
  tenant, and check `adp status`. Your box must already be running and use the
  approved SSH-enabled Dev Box image. The platform operator must have configured
  the Dev Box SSH grant role for that gateway (see [operator notes](ssh-audit.md)).
- Install OpenSSH (including `ssh-keygen`), AWS CLI v2 and the Session Manager
  plugin **on your laptop**. AWS CLI is only a transport for the temporary grant
  received from ADP; do **not** run `aws configure` or supply personal AWS keys.
- Know your box's name or ID from the Dev Box dashboard. The examples below use
  `assistant`, the default box name; replace it with your own name or ID.

## Terminal SSH

```bash
adp devbox ssh assistant
```

This writes or refreshes one `~/.ssh/config` host entry and opens an interactive
shell as `ec2-user`. Before accepting a **new** server host key, compare the
fingerprint with an independent value supplied by your platform operator.
Never bypass the unknown/changed-host warning with `StrictHostKeyChecking=no`.
If the box was replaced, obtain and verify its new fingerprint before updating
the saved key. The `HostKeyAlias` entry keeps this check stable across new
sessions and fails closed on an unexpected replacement.

To configure a host for another client without opening a shell:

```bash
adp devbox ssh-config assistant
```

The command prints the written alias. On a legacy/default deployment it is
`adp-devbox-assistant`; named deployments prefix their name, for example
`adp-devbox-dev-assistant`, to prevent another deployment's default selection
from silently changing the target. The entry pins the selected deployment URL,
deployment ID and tenant, contains **no** AWS keys or ADP token, and preserves
your unrelated SSH settings. If a host alias already belongs to something
else, the command refuses to overwrite it.

Each SSH connection through that alias runs a `ProxyCommand` that gets a fresh
owner grant, pushes the public key, then starts the `AWS-StartSSHSession` SSH
tunnel. The key is not left in `authorized_keys`. The IAM grant lasts at most
15 minutes, but a connection already established may continue after it expires;
do not mistake credential expiry for a maximum tunnel duration.

## VS Code, Kiro IDE and Kiro Crew

1. Run `adp devbox ssh-config assistant`, then make a **terminal** connection to
   the printed alias and verify the server's host key as described above. A
   headless IDE cannot perform your fingerprint check for you.
2. In VS Code, install/use Remote - SSH and select that alias in the **Connect
   to Host** menu. In Kiro IDE, use its Remote SSH host selector with the same
   alias and your local `~/.ssh/config`. Confirm a remote terminal runs as
   `ec2-user` on the box.
3. In Kiro Crew's **remote-host (SSH) mode**, select SSH configuration as the
   connection source and enter **the printed alias** (not an instance ID, IP
   address or dashboard URL). Connect and run a Crew command on the remote
   host; confirm its result and that the host is the expected Dev Box. Kiro
   Crew's UI/flag names vary by release, so use that release's remote-host
   selection rather than inventing an SSH flag.

Opening an IDE or Crew connection can start several SSH channels. Every new
connection refreshes the grant and short-lived public-key authorization.
Signing out of ADP or losing ownership prevents new sessions; an existing
session is not automatically revoked by token expiry. Close it explicitly.
For a changed host key, expired login, or unexpected refusal, do not disable
host-key checks: inspect the deployment selection, sign-in and host identity
with your operator.

The operator's [audit and acceptance notes](ssh-audit.md) explain what can be
verified after dev deployment. The local acceptance helper is
`modules/user-services/dev-box/tests/live_ssh_acceptance.sh`; it does not create
a box or deployment.
