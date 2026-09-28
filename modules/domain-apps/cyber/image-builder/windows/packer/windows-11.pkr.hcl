# =============================================================================
# Packer Template: Windows 11 Enterprise CAPE Guest Image (qcow2)
# =============================================================================
# Based on StefanScherer/packer-windows (commit 46f9305), converted from JSON
# to HCL2 and retargeted to QEMU/KVM with qcow2 output for CAPE sandboxing.
#
# Key changes from upstream:
# - QEMU builder instead of Hyper-V/VMware/VirtualBox
# - qcow2 output format (CAPE uses libvirt/KVM)
# - VirtIO drivers loaded via secondary CD-ROM
# - CAPE provisioning script appended as final step
# - User changed from vagrant to cape, hostname WIN-USER01
# =============================================================================

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "iso_url" {
  type        = string
  description = "Windows 11 Enterprise Evaluation ISO URL"
  # 23H2 Enterprise Evaluation (English, x64)
  default = "https://software-download.microsoft.com/download/sg/22631.2861.231204-0540.23h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso"
}

variable "iso_checksum" {
  type        = string
  description = "SHA256 checksum of the Windows ISO"
  default     = "none"
}

variable "virtio_iso_url" {
  type        = string
  description = "VirtIO drivers ISO URL (Fedora stable)"
  default     = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
}

variable "output_dir" {
  type    = string
  default = "/opt/windows-build/output"
}

variable "disk_size" {
  type    = string
  default = "61440"
}

variable "memory" {
  type    = string
  default = "4096"
}

variable "cpus" {
  type    = number
  default = 4
}

variable "winrm_username" {
  type    = string
  default = "cape"
}

variable "winrm_password" {
  type    = string
  default = "cape"
}

variable "winrm_timeout" {
  type    = string
  default = "4h"
}

# ---------------------------------------------------------------------------
# Source: QEMU/KVM
# ---------------------------------------------------------------------------

source "qemu" "windows-11" {
  iso_url      = var.iso_url
  iso_checksum = var.iso_checksum

  # VirtIO drivers ISO as secondary CD-ROM (drive E: in Autounattend.xml)
  cd_files = []
  qemuargs = [
    ["-drive", "file=${var.virtio_iso_url},media=cdrom,index=3"]
  ]

  # Floppy drive — Autounattend.xml + bootstrap scripts (StefanScherer pattern)
  # cape-configure-winrm.ps1 is a CAPE-specific companion that enforces WinRM
  # config via Group Policy keys + scheduled task (fixes Win11 23H2 regression
  # where Windows Security resets Basic auth after first logon). See #306.
  floppy_files = [
    "../answer_files/Autounattend.xml",
    "../scripts/fixnetwork.ps1",
    "../scripts/disable-screensaver.ps1",
    "../scripts/disable-winrm.ps1",
    "../scripts/enable-winrm.ps1",
    "../scripts/cape-configure-winrm.ps1",
    "../scripts/microsoft-updates.bat",
    "../scripts/win-updates.ps1"
  ]

  # Output
  format           = "qcow2"
  output_directory = var.output_dir
  vm_name          = "win11-cape.qcow2"

  # Machine configuration
  accelerator  = "kvm"
  machine_type = "pc"
  cpus         = var.cpus
  memory       = var.memory
  disk_size    = var.disk_size
  headless     = true
  disk_interface = "virtio"
  net_device     = "virtio-net"

  # WinRM communicator (StefanScherer's pattern)
  communicator   = "winrm"
  winrm_username = var.winrm_username
  winrm_password = var.winrm_password
  winrm_timeout  = var.winrm_timeout

  # Boot — empty boot_command is intentional (StefanScherer pattern).
  # Windows installer reads Autounattend.xml from floppy automatically.
  boot_command = [""]
  boot_wait    = "2m"

  # Shutdown
  shutdown_command = "shutdown /s /t 10 /f /d p:4:1 /c \"Packer Shutdown\""
  shutdown_timeout = "15m"

  # VNC for debugging (disabled in headless mode but available if needed)
  vnc_port_min = 5900
  vnc_port_max = 5980
}

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

build {
  sources = ["source.qemu.windows-11"]

  # CAPE-specific provisioning — Python, CAPE agent, anti-evasion
  provisioner "powershell" {
    script = "../scripts/provision-cape.ps1"
  }

  # Disable WinRM before final image (security best practice)
  provisioner "powershell" {
    script = "../scripts/disable-winrm.ps1"
  }
}
