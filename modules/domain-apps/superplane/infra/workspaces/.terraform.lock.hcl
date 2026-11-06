# Reviewed Linux amd64 packages for the dedicated lifecycle worker and CI.
# SHA-256 identities come from HashiCorp's versioned release SHA256SUMS:
# https://releases.hashicorp.com/terraform-provider-aws/6.65.0/terraform-provider-aws_6.65.0_SHA256SUMS
# https://releases.hashicorp.com/terraform-provider-tls/4.4.1/terraform-provider-tls_4.4.1_SHA256SUMS
# h1 identities were verified with `terraform providers lock -platform=linux_amd64`
# against these signed archives. Terraform 1.9.8 needs the extracted-package hashes
# after read-only initialization; archive hashes alone do not validate its cache.

provider "registry.terraform.io/hashicorp/aws" {
  version     = "6.65.0"
  constraints = "6.65.0"
  hashes = [
    "h1:RjeO6m/SvlhGUCDrwTdj99kJhKl/rC1zE9B5mi3YhVw=",
    "zh:718a880d81bfd9af7e297ed3d7bf98d1febebe8b9ebe3333854a4c17f2c4de09",
  ]
}

provider "registry.terraform.io/hashicorp/tls" {
  version     = "4.4.1"
  constraints = "4.4.1"
  hashes = [
    "h1:1Zk4TqZwWmUZHfGTtM/vw8EKoWecYboMRqwhNaoTopc=",
    "zh:debc830ec123f27944c6170d76cb4ea3542845896960bb9fc530702774df328e",
  ]
}
