# =============================================================================
# ECR repositories for the pinned Superplane images — Issue #5042 (U3).
# =============================================================================
# The three image-build lanes U2 owns (superplane-api-build.yml and siblings) push to
# `adp-superplane-api`, `adp-superplane-controller` and
# `adp-superplane-platform-monitor`. Those names are a contract read out of
# releases/superplane.lock.yaml by resolve_lock.py, so they are derived from the lock
# here rather than retyped — a rename in the lock that this module did not follow would
# otherwise surface as a push failure in a build lane, not as a plan diff.
#
# Owned here rather than in platform/infra's shared ECR module deliberately. Platform
# isolation: a domain app's repositories must be created and destroyed with the domain
# app. Adding three names to `var.ecr_repositories` in environments/dev/platform.tfvars
# would put them in platform state, which would mean tearing Superplane down either left
# them behind or required an apply against core platform state. That is the cyber-module
# teardown failure phase_superplane exists not to repeat.
# =============================================================================

locals {
  # Read from U2's lock so the repository names cannot drift from what the build lanes
  # push to.
  lock = yamldecode(file("${path.module}/../../releases/superplane.lock.yaml"))

  # WHY THE INVENTORY IS A UNION AND NOT `pending_images` ALONE.
  #
  # Reading only `pending_images` was a defect (PR #5283 review, finding 2), and the
  # failure mode is specifically the SUCCESS case. U2's merged release contract moves an
  # entry OUT of `pending_images` and into `images` + `image_sources` when its digest is
  # resolved. So the moment the first Superplane image is actually built and promoted, its
  # repository left this list — and Terraform planned to DESTROY the repository holding
  # the image that had just been pushed to it. Reproduced with `terraform console` against
  # a post-promotion lock: three repositories before, two after.
  #
  # It also fails in a way that hides itself. A nonempty ECR repository refuses deletion,
  # so the first release does not quietly lose a repository — it breaks apply, at the one
  # moment somebody is trying to ship. And on a repository that happened to be empty, it
  # would succeed and take the lifecycle policy with it.
  #
  # The union keys off BOTH maps, so an entry's repository survives promotion in either
  # direction. `image_sources` is the promoted-side source of truth for build metadata;
  # entries there that carry no `ecr_repository` (skypilot-api, pulled from Docker Hub by
  # digest) are skipped by the same condition that skips them in `pending_images` — the
  # external registry stays external and gets no ECR repository of its own.
  ecr_repository_candidates = merge(
    { for name, entry in try(local.lock.pending_images, {}) : name => try(entry.ecr_repository, null) },
    { for name, entry in try(local.lock.image_sources, {}) : name => try(entry.ecr_repository, null) },
  )

  superplane_ecr_repositories = sort(distinct([
    for name, repository in local.ecr_repository_candidates :
    repository
    if repository != null
  ]))

  # Ownership check, not decoration. `for_each` over a set silently deduplicates, so two
  # lock entries naming the same repository would collapse to one resource and the second
  # image would push into the first's repository unnoticed. Compare pre-dedup count with
  # post-dedup count to catch that.
  ecr_repository_names_prededup = [
    for name, repository in local.ecr_repository_candidates : repository if repository != null
  ]

  # Every repository this module creates must be domain-owned. Without this, a lock edit
  # naming `adp-gateway` would make this module create — and, on destroy, delete — a
  # repository belonging to the gateway. That is exactly the "a separate state key alone
  # does not prove resource isolation" case in the platform-isolation requirement.
  ecr_repositories_foreign = [
    for repository in local.superplane_ecr_repositories :
    repository
    if !startswith(repository, "adp-superplane-")
  ]
}

# Fail the plan rather than the apply. Both conditions below describe a lock that cannot
# be deployed safely; a `check` block would only warn, and these must stop the lane.
resource "terraform_data" "ecr_inventory_guard" {
  lifecycle {
    precondition {
      condition = length(local.ecr_repository_names_prededup) == length(local.superplane_ecr_repositories)
      error_message = join(" ", [
        "Two or more entries in releases/superplane.lock.yaml name the same ecr_repository.",
        "for_each would deduplicate them, so one image would push into another's repository.",
        "Give each image its own repository in the lock.",
      ])
    }
    precondition {
      condition = length(local.ecr_repositories_foreign) == 0
      error_message = join(" ", [
        "releases/superplane.lock.yaml names ECR repositories outside the domain's",
        "`adp-superplane-` prefix: ${join(", ", local.ecr_repositories_foreign)}.",
        "This module must not create or destroy repositories it does not own",
        "(platform isolation requirement, 2026-09-16).",
      ])
    }
  }
}

resource "aws_ecr_repository" "superplane" {
  for_each = toset(local.superplane_ecr_repositories)

  name = each.value

  # Immutable tags: a deploy resolves images by digest (U2's pinning), and mutable tags
  # would let a tag be repointed at a different image after the digest was recorded.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # AWS-managed encryption. A domain-app CMK is not created here: it would be an
  # unbudgeted resource, and the issue's cost footprint is explicitly bounded to what
  # the accepted scope names.
  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keep the repositories from growing without bound. Untagged images accumulate on every
# rebuild of a digest-pinned image, and nothing references them once superseded.
resource "aws_ecr_lifecycle_policy" "superplane" {
  for_each = aws_ecr_repository.superplane

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 30 most recent tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 30
        }
        action = { type = "expire" }
      },
    ]
  })
}
