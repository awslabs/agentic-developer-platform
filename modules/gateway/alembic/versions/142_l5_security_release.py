"""Install module-owned L5 tables on the gateway's single migration lineage."""

revision = "142_l5_security_release"
down_revision = "141_normalize_environments"
branch_labels = None
depends_on = None


def upgrade():
    from l5_services.migrations.versions import (
        l5_admission_recovery,
        l5_configurations,
        l5_devops_resource_observation,
        l5_devops_space_intents,
        l5_events,
        l5_runs_and_work,
        l5_worker_attempts,
        l5_worker_evidence,
        l5_worker_fences,
        l5_worker_repair,
        l5_worker_runtime,
    )

    for migration in (
        l5_configurations,
        l5_runs_and_work,
        l5_admission_recovery,
        l5_worker_evidence,
        l5_worker_runtime,
        l5_worker_attempts,
        l5_worker_repair,
        l5_worker_fences,
        l5_events,
        l5_devops_space_intents,
        l5_devops_resource_observation,
    ):
        migration.upgrade()


def downgrade():
    raise RuntimeError("The L5 release retains work history and cannot safely be reversed")
