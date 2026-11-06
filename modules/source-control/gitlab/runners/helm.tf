resource "kubernetes_config_map_v1" "manager_prestart" {
  for_each = local.active_group_bindings
  metadata {
    name      = "manager-prestart"
    namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
  }
  data = { "prepare_manager.py" = file("${path.module}/scripts/prepare_manager.py") }
}

resource "kubernetes_config_map_v1" "manager_loader" {
  for_each = local.active_group_bindings
  metadata {
    name      = "manager-loader"
    namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
  }
  data = { "load_credentials.py" = file("${path.module}/bootstrap/load_credentials.py") }
}

resource "kubernetes_config_map_v1" "manager_plugin_installer" {
  for_each = local.active_group_bindings
  metadata {
    name      = "manager-plugin-installer"
    namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
  }
  data = { "install_plugin.py" = file("${path.module}/bootstrap/install_plugin.py") }
}

locals {
  chart_archive = var.runner_chart_archive == "" ? "${path.module}/assets/gitlab-runner-0.94.2.tgz" : var.runner_chart_archive
  bootstrap_resources = {
    requests = { cpu = "100m", memory = "128Mi" }
    limits   = { cpu = "250m", memory = "256Mi" }
  }
  manager_values = {
    for group_id, binding in local.active_group_bindings :
    group_id => yamlencode({
      image              = { registry = "", repository = "gitlab/gitlab-runner", tag = trimprefix(local.release_lock.manager_image, "gitlab/gitlab-runner:") }
      gitlabUrl          = data.aws_ssm_parameter.runner_url.value
      replicas           = 1
      strategy           = { type = "Recreate" }
      podSecurityContext = { fsGroup = 10001 }
      resources = {
        requests = { cpu = "500m", memory = "512Mi" }
        limits   = { cpu = "1", memory = "1Gi" }
      }
      securityContext = {
        allowPrivilegeEscalation = false
        runAsNonRoot             = true
        runAsUser                = 10001
        runAsGroup               = 10001
        capabilities             = { drop = ["ALL"] }
        seccompProfile           = { type = "RuntimeDefault" }
      }
      concurrent                   = 2
      rbac                         = { create = false }
      serviceAccount               = { create = false, name = "gitlab-runner" }
      automountServiceAccountToken = true
      extraEnv = {
        HOME                  = "/home/gitlab-runner"
        CI_SERVER_TLS_CA_FILE = "/secrets/ca.pem"
        PATH                  = "/runner-plugin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      }
      hostAliases = [{
        ip        = try(data.aws_network_interface.endpoint["${group_id}/cache"].private_ip, "0.0.0.0")
        hostnames = ["s3.${var.aws_region}.amazonaws.com"]
      }]
      deploymentInitContainers = [{
        name            = "manager-plugin"
        image           = try(local.release_lock.bootstrap_image, "")
        resources       = local.bootstrap_resources
        imagePullPolicy = "IfNotPresent"
        command         = ["python3", "/opt/manager/install_plugin.py"]
        args            = ["/opt/fleeting/fleeting-plugin-aws", "/runner-plugin", try(local.release_lock.fleeting_plugin.sha256, "")]
        securityContext = {
          allowPrivilegeEscalation = false
          readOnlyRootFilesystem   = true
          runAsNonRoot             = true
          runAsUser                = 10001
          runAsGroup               = 10001
          capabilities             = { drop = ["ALL"] }
          seccompProfile           = { type = "RuntimeDefault" }
        }
        volumeMounts = [
          { name = "manager-plugin-installer", mountPath = "/opt/manager", readOnly = true },
          { name = "manager-plugin", mountPath = "/runner-plugin" }
        ]
        }, {
        name            = "manager-credentials"
        image           = try(local.release_lock.bootstrap_image, "")
        imagePullPolicy = "IfNotPresent"
        command         = ["python3", "/opt/manager/load_credentials.py"]
        args = [
          "--group-id", group_id, "--tenant-id", binding.tenant_id,
          "--runner-url", data.aws_ssm_parameter.runner_url.value,
          "--tags", join(",", binding.tags), "--access-level", binding.access_level,
          "--token-arn", "adp/${var.environment}/gitlab-runner/${group_id}",
          "--ca-arn", data.aws_ssm_parameter.ca_secret_arn.value,
          "--output-dir", "/runner-credentials", "--certs-dir", "/runner-certs",
          "--system-id-dir", "/runner-config"
        ]
        resources = local.bootstrap_resources
        securityContext = {
          allowPrivilegeEscalation = false
          readOnlyRootFilesystem   = true
          runAsNonRoot             = true
          runAsUser                = 10001
          runAsGroup               = 10001
          capabilities             = { drop = ["ALL"] }
          seccompProfile           = { type = "RuntimeDefault" }
        }
        volumeMounts = [
          { name = "manager-loader-source", mountPath = "/opt/manager", readOnly = true },
          { name = "manager-secrets", mountPath = "/runner-credentials" },
          { name = "manager-certs", mountPath = "/runner-certs" },
          { name = "etc-gitlab-runner", mountPath = "/runner-config" }
        ]
      }]
      preEntrypointScript = file("${path.module}/templates/manager-prestart.sh")
      volumes = [
        { name = "manager-guard", emptyDir = { medium = "Memory" } },
        { name = "manager-guard-source", configMap = { name = kubernetes_config_map_v1.manager_prestart[group_id].metadata[0].name } },
        { name = "manager-loader-source", configMap = { name = kubernetes_config_map_v1.manager_loader[group_id].metadata[0].name } },
        { name = "manager-plugin-installer", configMap = { name = kubernetes_config_map_v1.manager_plugin_installer[group_id].metadata[0].name } },
        { name = "manager-plugin", emptyDir = { medium = "Memory" } },
        { name = "manager-secrets", emptyDir = { medium = "Memory" } },
        { name = "manager-certs", emptyDir = { medium = "Memory" } }
      ]
      volumeMounts = [
        { name = "manager-guard", mountPath = "/runner-guard" },
        { name = "manager-plugin", mountPath = "/runner-plugin", readOnly = true },
        { name = "manager-secrets", mountPath = "/secrets", readOnly = true },
        { name = "manager-certs", mountPath = "/home/gitlab-runner/.gitlab-runner/certs", readOnly = true }
      ]
      extraContainers = [{
        name            = "manager-guard"
        image           = try(local.release_lock.bootstrap_image, "")
        resources       = local.bootstrap_resources
        imagePullPolicy = "IfNotPresent"
        command         = ["python3", "/opt/manager/prepare_manager.py"]
        args            = ["--environment", var.environment, "--region", var.aws_region, "--group-id", group_id]
        readinessProbe = {
          exec             = { command = ["python3", "-c", "from pathlib import Path; import time; p=Path('/runner-guard'); assert (p/'ack').read_text() == (p/'request').read_text(); assert all(0 <= time.time()-(p/n).stat().st_mtime <= 30 for n in ('alive','lease'))"] }
          periodSeconds    = 5
          timeoutSeconds   = 2
          failureThreshold = 3
        }
        securityContext = {
          allowPrivilegeEscalation = false
          readOnlyRootFilesystem   = true
          runAsNonRoot             = true
          runAsUser                = 10001
          runAsGroup               = 10001
          capabilities             = { drop = ["ALL"] }
          seccompProfile           = { type = "RuntimeDefault" }
        }
        volumeMounts = [
          { name = "manager-guard", mountPath = "/runner-guard" },
          { name = "manager-guard-source", mountPath = "/opt/manager", readOnly = true },
          { name = "etc-gitlab-runner", mountPath = "/runner-config", readOnly = true },
          { name = "manager-secrets", mountPath = "/runner-credentials", readOnly = true }
        ]
      }]
      runners = {
        name        = "gitlab-group-${group_id}"
        executor    = "docker-autoscaler"
        runUntagged = false
        tags        = join(",", binding.tags)
        config = templatefile("${path.module}/templates/runner.toml.tftpl", {
          group_id     = group_id
          aws_region   = var.aws_region
          asg_name     = "adp-${var.environment}-gitlab-job-${group_id}"
          cache_bucket = "adp-${var.environment}-gitlab-runners-${data.aws_caller_identity.current.account_id}"
          image        = var.job_image_digest
          helper_image = try(local.release_lock.helper_image, "")
          gitlab_host  = local.private_gitlab_hostname
          gitlab_ip    = try(data.aws_network_interface.endpoint["${group_id}/gitlab"].private_ip, "0.0.0.0")
          cache_ip     = try(data.aws_network_interface.endpoint["${group_id}/cache"].private_ip, "0.0.0.0")
        })
        cache = { disableGlobalCredentialsExport = true }
      }
    })

  }
}

resource "helm_release" "manager" {
  for_each  = local.active_group_bindings
  name      = "gitlab-runner"
  namespace = kubernetes_namespace_v1.manager[each.key].metadata[0].name
  chart     = local.chart_archive
  wait      = true
  atomic    = true
  timeout   = 600

  values     = [local.manager_values[each.key]]
  depends_on = [kubernetes_service_account_v1.manager, kubernetes_config_map_v1.manager_prestart, kubernetes_config_map_v1.manager_loader, kubernetes_config_map_v1.manager_plugin_installer, aws_autoscaling_group.job, aws_s3_bucket.cache, aws_ssm_parameter.manager_alive, aws_ssm_parameter.registration_receipt, terraform_data.release_ready]

  lifecycle {
    precondition {
      condition     = fileexists(local.chart_archive) && try(filesha256(local.chart_archive) == "6a63ce370db021252f4f6f0414a4ff1fb09f8105d683dd8539ee670263944f21", false)
      error_message = "The verified local GitLab Runner chart 0.94.2 must be supplied before manager activation."
    }
  }
}

data "helm_template" "manager" {
  for_each  = local.active_group_bindings
  name      = "gitlab-runner"
  namespace = each.value.manager_namespace
  chart     = local.chart_archive
  values    = [local.manager_values[each.key]]
}
