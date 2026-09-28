terraform {
  # >= 1.9 for the same reason as ../control-plane/versions.tf, and one more:
  #
  #   1.6  `terraform test` / .tftest.hcl files at all
  #   1.7  `mock_provider`, which every test here relies on to run without credentials
  #   1.9  cross-variable `validation` conditions — variables.tf needs these in three
  #        places: the networking-mode rules refer to the owned/supplied inputs from
  #        inside each other's validation, the endpoint rule refers to
  #        var.cluster_endpoint_public_access from inside the CIDR list's validation, and
  #        the scaling limits cross-check minimum, desired and maximum capacity.
  required_version = "= 1.9.8"

  backend "s3" {
    # Configured via -backend-config at init. Deliberately empty, and here the stake is
    # higher than it is for the control plane: this module is instantiated once PER
    # WORKSPACE, so the state key is what keeps one workspace's apply from planning a
    # destroy against another workspace's cluster.
    #
    # A bucket or key written inline would make that isolation depend on editing this
    # file, and every workspace would share whatever was written. See
    # `output "state_key_convention"` in outputs.tf for the required shape, and
    # tests/test_workspace_backend_state_key.py, which reads this file as text to assert the block
    # stays variable-free. That check cannot be a `terraform test` assertion: the backend
    # is resolved at init and never enters the plan graph.
    #
    #   key = "<environment>/modules/superplane-workspaces/v2/<org_id>/<workspace_id>/terraform.tfstate"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.65.0"
    }

    # Read-only, and used for exactly one thing: reading the CA thumbprint of this cluster's
    # OIDC issuer so the IAM OIDC provider in eks.tf can trust it (see the note there on why
    # per-workload identity matters). It creates nothing and needs no credentials.
    #
    # The alternative — hardcoding a known root CA thumbprint — was rejected: that value
    # changes when AWS rotates the issuer's certificate chain, and a stale thumbprint breaks
    # every IRSA role in the workspace at once, with an error that names none of this.
    tls = {
      source  = "hashicorp/tls"
      version = "= 4.4.1"
    }
  }
}
