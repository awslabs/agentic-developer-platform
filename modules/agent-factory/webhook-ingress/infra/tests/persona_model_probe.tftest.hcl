# PMM-03 (#5420): prove the scheduled probe is inert by default and that its
# pod preserves the single-attempt, worker-NetworkPolicy and IRSA boundaries.
# Plan-only with mocked providers; live enablement belongs to PMM-09.

mock_provider "aws" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "tls" {}

override_data {
  target          = data.aws_caller_identity.current
  override_during = plan
  values = {
    account_id = "123456789012"
  }
}

override_data {
  target          = data.aws_region.current
  override_during = plan
  values = {
    name = "us-east-1"
  }
}

override_data {
  target          = data.aws_iam_policy_document.dynamodb_kms
  override_during = plan
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_data {
  target          = data.aws_iam_policy_document.cloudwatch_kms
  override_during = plan
  values = {
    json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
  }
}

override_data {
  target          = data.aws_eks_cluster.main
  override_during = plan
  values = {
    identity = [{
      oidc = [{
        issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLE"
      }]
    }]
    certificate_authority = [{
      data = "TFNUQVJUQ0VSVElGSUNBVEU="
    }]
  }
}

run "probe_is_inert_and_bounded_by_default" {
  command = plan

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].suspend == true
    error_message = "The PMM-03 CronJob must ship suspended; PMM-09 owns live enablement."
  }

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].concurrency_policy == "Forbid"
    error_message = "Concurrent probe cycles could exceed the durable cycle spend reservation."
  }

  assert {
    condition = (
      kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].parallelism == 1 &&
      kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].completions == 1 &&
      kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].backoff_limit == 0
    )
    error_message = "A probe tick must be one pod, one completion and zero Kubernetes retries."
  }

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].template[0].spec[0].service_account_name == kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
    error_message = "The probe must reuse agent-scaledjob-sa and its reviewed IRSA permissions."
  }

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].template[0].metadata[0].labels["app.kubernetes.io/name"] == "agent-scaledjob"
    error_message = "The probe pod must match the agent-scaledjob egress NetworkPolicy."
  }

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].job_template[0].spec[0].template[0].spec[0].container[0].command == tolist(["node", "/app/dist/invocability-probe/index.js"])
    error_message = "The CronJob must invoke the non-interactive SDK probe entrypoint from the production agent image."
  }

}

run "explicit_enable_only_unsuspends_scheduler" {
  command = plan

  variables {
    persona_model_probe_enabled = true
  }

  assert {
    condition     = kubernetes_cron_job_v1.persona_model_probe.spec[0].suspend == false
    error_message = "An explicitly enabled PMM-03 scheduler must be unsuspended."
  }

}
