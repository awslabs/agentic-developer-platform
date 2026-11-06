# GitLab module infrastructure

The GitLab instance and runner fleet have their own Terraform roots in this
module. They use the deployment's existing VPC, private subnets and EKS cluster.
Separate state keeps runner teardown from deleting the GitLab instance,
credentials, build artifacts or shared platform resources.

| Terraform root | Infrastructure it owns |
| --- | --- |
| [`infra/`](infra/) | GitLab EC2 instance, private ALB and security groups, storage and backups, SSO integration, secrets and discovery references; scoped registrar/exporter policies |
| [`runners/ca-exporter/infra/`](runners/ca-exporter/infra/) | Separate CA-exporter role and exact operator trust |
| [`runners/image/infra/`](runners/image/infra/) | Temporary AMI builder's IAM role, limited image-pull/SSM policy, instance profile and optional private builder network group |
| [`runners/builder/infra/`](runners/builder/infra/) | Dedicated CodeBuild project and role, and immutable runtime-image repositories |
| [`runners/`](runners/) | EKS runner managers, per-group IAM and namespaces, private EC2 job autoscaling groups, job network controls, cache and reconciliation |

The build roots currently describe the dev runner release; their resource
names and build-driver checks must be reviewed before targeting another
environment. The exporter root accepts its account, region, environment and
operator roles as inputs. Private variable files, backend configuration and
saved plans are deployment artifacts, kept outside the public checkout.

## Shared platform inputs

The existing VPC/EKS cluster, state/evidence bucket, CodeBuild permission
boundary and protected GitHub operator roles remain owned by their platform
stacks. Pass their reviewed identifiers to the GitLab roots. The shared trusted
build dispatcher must authorize this module's exact build project and image
repositories through its existing automation configuration; do not create a
second owner for that shared IAM policy.

Use `runners/builder/infra/` for the dedicated builder. The legacy optional
platform `enable_gitlab_runner_build` path can name the same resources and
must not be enabled alongside it. Existing resources require ownership/state
reconciliation before using a different root.

GitHub workflows live in the repository's `.github/workflows` directory and
invoke the module's code. Adding Terraform source does not supply the
operator's IAM write or PassRole permissions. Those remain explicit account
setup inputs controlled by the access owner.

## Setup order

1. Prepare the module's exporter, AMI-builder and CodeBuild prerequisite roots
   with the selected account and independent backend keys. Review their saved
   plans and have the authorized setup identity apply them.
   Use a separate key for each root, including the AMI-builder identity root
   (for example, `<environment>/modules/gitlab-runner-image-builder/terraform.tfstate`).
   Retain any existing local bootstrap state and review its migration before
   selecting an empty remote backend.
2. Configure the instance root with the exporter output as
   `runner_ca_exporter_role_name`, an existing approved registrar role, and
   actual group bindings. The instance root attaches certificate/CA-secret
   permissions and denies exporter access to administrator and group tokens.
3. Follow [registration and CA verification](docs/runner-registration-live.md),
   then [the runner runbook](docs/runners.md) to build and qualify images,
   configure the protected workflow inputs and deploy the runner stack.
4. Complete the separate live-readiness and live-qualification steps. Source
   tests do not establish a deployed or qualified runner fleet.

For the exporter, see [its setup procedure](docs/runner-ca-exporter-infrastructure.md).
