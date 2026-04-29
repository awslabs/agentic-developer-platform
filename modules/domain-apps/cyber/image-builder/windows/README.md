# Windows 11 CAPE-ready qcow2 Image Builder

Automated pipeline for building Windows 11 Enterprise Evaluation qcow2 images
for CAPE malware analysis sandboxing.

## Provenance

This pipeline is based on [StefanScherer/packer-windows](https://github.com/StefanScherer/packer-windows),
vendored at commit **`46f9305`** (2024 — latest as of vendoring date 2026-04-29).

The following files are copied verbatim from that repository:

- `answer_files/Autounattend.xml` — from `answer_files/11/Autounattend.xml`
- `scripts/enable-winrm.ps1` — from `scripts/enable-winrm.ps1`
- `scripts/disable-winrm.ps1` — from `scripts/disable-winrm.ps1`
- `scripts/fixnetwork.ps1` — from `scripts/fixnetwork.ps1`
- `scripts/disable-screensaver.ps1` — from `scripts/disable-screensaver.ps1`
- `scripts/microsoft-updates.bat` — from `scripts/microsoft-updates.bat`
- `scripts/win-updates.ps1` — from `scripts/win-updates.ps1`

**Do NOT modify the vendored scripts** — they handle subtle WinRM timing,
TPM/SecureBoot bypass, and network detection issues that took years to debug.

## Our additions

- `packer/windows-11.pkr.hcl` — HCL2 Packer template targeting QEMU/KVM with qcow2 output
  (StefanScherer's original is JSON targeting Hyper-V/VMware/VirtualBox)
- `scripts/provision-cape.ps1` — CAPE guest agent, Python 3.12, anti-evasion hardening
- `build-pipeline.sh` — Orchestrator that runs on the build host via SSM

## How it works

1. GitHub Actions workflow spins up a c8i.4xlarge build host (nested virt enabled)
2. `build-pipeline.sh` runs via SSM on the host
3. Packer downloads the Windows 11 ISO + VirtIO drivers ISO
4. Autounattend.xml handles unattended install with TPM/SecureBoot bypass
5. WinRM comes up after first-logon scripts complete
6. Packer provisions: CAPE agent, Python, anti-evasion baseline
7. Final qcow2 uploaded to `s3://adp-dev-cape-assets/win11-cape-<date>.qcow2`
8. CAPE host auto-registers the new image

## Build schedule

- Weekly: Sundays 02:00 UTC (catches Microsoft patch/URL breakage early)
- On push: any change to `modules/domain-apps/cyber/image-builder/windows/`
- Manual: `workflow_dispatch`

## License

StefanScherer/packer-windows is MIT-licensed. See upstream repository for details.
