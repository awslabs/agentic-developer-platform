# =============================================================================
# Packer Template: Ubuntu 22.04 CAPE Guest Image
# =============================================================================
# Builds a CAPE-ready Ubuntu 22.04 qcow2 from the official cloud image.
# Uses cloud-init seed ISO for first-boot user configuration and SSH access.
#
# Key differences from the Windows template:
# - No ISO install — cloud image is already a bootable disk (disk_image=true)
# - SSH communicator instead of WinRM
# - cloud-init via secondary cdrom (NoCloud datasource)
# - Much faster build (~15-25 min vs 45+ min for Windows)
# =============================================================================

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "output_dir" {
  type    = string
  default = "/opt/linux-build/output"
}

source "qemu" "ubuntu-cape" {
  # Pre-built cloud image — not an installer ISO.
  # Pin to jammy/current for reproducibility within the 22.04 LTS track.
  iso_url      = "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
  iso_checksum = "file:https://cloud-images.ubuntu.com/jammy/current/SHA256SUMS"

  # disk_image = true tells Packer the iso_url is already a bootable disk,
  # not an installer — copies it as the working disk and boots directly.
  disk_image = true
  format     = "qcow2"

  # Inject cloud-init via secondary cdrom (NoCloud seed ISO)
  cd_files = ["../cloud-init/user-data", "../cloud-init/meta-data"]
  cd_label = "cidata"

  # Machine configuration — SeaBIOS, matches Windows pattern
  machine_type = "pc"
  accelerator  = "kvm"
  cpus         = 2
  memory       = 2048
  disk_size    = "10G"
  headless     = true

  # Networking — user-mode (SLIRP), no bridge needed during build
  net_device = "virtio-net"

  # SSH communicator — cloud-init sets up the user + password
  communicator   = "ssh"
  ssh_username   = "cape"
  ssh_password   = "cape"
  ssh_timeout    = "10m"

  # Give cloud-init time to finish before Packer tries SSH
  boot_wait = "45s"

  shutdown_command  = "sudo systemctl poweroff"
  disk_compression  = true
  output_directory  = var.output_dir
  vm_name           = "ubuntu-cape-v1.qcow2"
}

build {
  sources = ["source.qemu.ubuntu-cape"]

  # Stage CAPE agent (downloaded by build-pipeline.sh before packer runs)
  provisioner "file" {
    source      = "/opt/linux-build/payload/agent.py"
    destination = "/tmp/agent.py"
  }

  # Install system dependencies
  provisioner "shell" {
    script = "../scripts/01-install-deps.sh"
  }

  # Install CAPE agent as systemd service + anti-evasion baseline
  provisioner "shell" {
    script = "../scripts/02-setup-cape-agent.sh"
  }
}
