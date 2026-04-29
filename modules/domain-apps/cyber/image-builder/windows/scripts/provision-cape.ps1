# =============================================================================
# provision-cape.ps1 — CAPE-specific provisioning for Windows 11 sandbox VM
# =============================================================================
# Runs AFTER StefanScherer's base scripts have completed (WinRM is up,
# Windows Updates installed, screensaver disabled, etc.)
#
# This script adds:
# 1. Python 3.12 (silent install, system-wide)
# 2. CAPE guest agent (scheduled task at boot, runs as SYSTEM)
# 3. Anti-evasion hardening (Defender off, sleep off, firewall off)
# 4. Plausible user artifacts (documents in common locations)
# =============================================================================

$ErrorActionPreference = "Continue"
$log = "C:\cape\provision.log"
New-Item -Path "C:\cape" -ItemType Directory -Force | Out-Null

# ---------------------------------------------------------------------------
# 1. Python 3.12 (silent)
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Installing Python 3.12..." | Out-File $log -Append
Invoke-WebRequest "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" `
  -OutFile "C:\cape\python-installer.exe"
Start-Process "C:\cape\python-installer.exe" `
  -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_test=0" -Wait
"$(Get-Date -Format o) Python installed." | Out-File $log -Append

# ---------------------------------------------------------------------------
# 2. CAPE guest agent
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Fetching CAPE agent..." | Out-File $log -Append
Invoke-WebRequest "https://raw.githubusercontent.com/kevoreilly/CAPEv2/master/agent/agent.py" `
  -OutFile "C:\cape\agent.py"
schtasks /create /tn "CAPEAgent" /tr "C:\Python312\python.exe C:\cape\agent.py" `
  /sc ONSTART /ru SYSTEM /rl HIGHEST /f
"$(Get-Date -Format o) CAPE agent scheduled task created." | Out-File $log -Append

# ---------------------------------------------------------------------------
# 3. Anti-evasion: Windows Defender
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Disabling Windows Defender..." | Out-File $log -Append
Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction SilentlyContinue
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender" `
  -Name "DisableAntiSpyware" -Value 1 -Type DWord

# ---------------------------------------------------------------------------
# 4. Anti-evasion: Power management (CAPE samples may run 30+ min)
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Disabling sleep/hibernate..." | Out-File $log -Append
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /h off

# ---------------------------------------------------------------------------
# 5. Anti-evasion: Firewall off (sandbox network has its own iptables)
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Disabling firewall..." | Out-File $log -Append
netsh advfirewall set allprofiles state off

# ---------------------------------------------------------------------------
# 6. Anti-evasion: Disable UAC
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Disabling UAC..." | Out-File $log -Append
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
  -Name "EnableLUA" -Value 0 -Type DWord -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 7. Anti-evasion: Disable Windows Update service
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Disabling Windows Update service..." | Out-File $log -Append
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Set-Service wuauserv -StartupType Disabled -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 8. Anti-evasion: Hostname rename (plausible workstation name)
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Renaming computer to WIN-USER01..." | Out-File $log -Append
Rename-Computer -NewName "WIN-USER01" -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 9. Plausible user artifacts (anti-sandbox detection)
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Seeding user artifacts..." | Out-File $log -Append
$docPath = "C:\Users\cape\Documents"
$dlPath = "C:\Users\cape\Downloads"
New-Item -Path $docPath -ItemType Directory -Force | Out-Null
New-Item -Path $dlPath -ItemType Directory -Force | Out-Null
"q1 report draft - confidential" | Out-File "$docPath\Q1-Budget-Report.docx"
"invoice 2026-03 payment received" | Out-File "$dlPath\invoice-march-2026.pdf"
"meeting notes - team sync" | Out-File "$docPath\meeting-notes-04-15.txt"
"project plan v2" | Out-File "$docPath\project-plan.xlsx"

# ---------------------------------------------------------------------------
# 10. Cleanup installer
# ---------------------------------------------------------------------------
"$(Get-Date -Format o) Cleaning up installer..." | Out-File $log -Append
Remove-Item "C:\cape\python-installer.exe" -Force -ErrorAction SilentlyContinue

"$(Get-Date -Format o) CAPE provision complete." | Out-File $log -Append
Write-Output "CAPE provisioning finished successfully."
