# =============================================================================
# cape-configure-winrm.ps1 — CAPE-specific WinRM hardener
# =============================================================================
# Companion to StefanScherer's enable-winrm.ps1 that addresses the Windows 11
# 23H2 regression where Windows Security re-applies policies that override
# WinRM Basic auth and AllowUnencrypted settings after first logon.
#
# This script:
# 1. Sets Group Policy registry keys that CANNOT be overridden by automatic
#    Windows Security hardening (policy keys take precedence over service config)
# 2. Configures WinRM service directly (belt-and-suspenders)
# 3. Creates a scheduled task "CAPE-WinRM-Configure" that re-applies config
#    at system startup and every 2 minutes (ensures settings persist across
#    reboots triggered by Windows Update)
#
# Invoked from Autounattend.xml Order 15 (before Windows Update runs) so the
# scheduled task survives the entire update reboot cycle.
#
# SAFE TO RUN MULTIPLE TIMES (idempotent).
# =============================================================================

$ErrorActionPreference = "Continue"
$LogFile = "C:\Windows\Temp\cape-winrm.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts $msg" | Out-File $LogFile -Append -Encoding UTF8
    Write-Output $msg
}

Log "=== cape-configure-winrm.ps1 starting ==="

# ---------------------------------------------------------------------------
# 1. Group Policy registry keys — these OVERRIDE service-level config and
#    prevent Windows Security from resetting WinRM settings.
# ---------------------------------------------------------------------------
Log "Setting WinRM Group Policy registry keys..."

# Service policies (server-side)
$svcPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinRM\Service"
New-Item -Path $svcPath -Force | Out-Null
Set-ItemProperty -Path $svcPath -Name "AllowBasic" -Value 1 -Type DWord
Set-ItemProperty -Path $svcPath -Name "AllowUnencryptedTraffic" -Value 1 -Type DWord
Set-ItemProperty -Path $svcPath -Name "AllowAutoConfig" -Value 1 -Type DWord
# IPv4Filter * = allow all IPv4 connections
Set-ItemProperty -Path $svcPath -Name "IPv4Filter" -Value "*" -Type String

# Client policies (for completeness)
$clientPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinRM\Client"
New-Item -Path $clientPath -Force | Out-Null
Set-ItemProperty -Path $clientPath -Name "AllowBasic" -Value 1 -Type DWord
Set-ItemProperty -Path $clientPath -Name "AllowUnencryptedTraffic" -Value 1 -Type DWord

Log "Group Policy keys set."

# ---------------------------------------------------------------------------
# 2. Set network profile to Private (required for Enable-PSRemoting)
# ---------------------------------------------------------------------------
Log "Setting network to Private..."
Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# 3. Configure WinRM service
# ---------------------------------------------------------------------------
Log "Configuring WinRM service..."

# Ensure service is set to auto-start
Set-Service winrm -StartupType Automatic -ErrorAction SilentlyContinue

# Start the service
Start-Service winrm -ErrorAction SilentlyContinue

# Enable PS Remoting (creates default listener if missing)
Enable-PSRemoting -Force -SkipNetworkProfileCheck -ErrorAction SilentlyContinue

# Configure WinRM settings
winrm quickconfig -q 2>$null
winrm quickconfig -transport:http 2>$null
winrm set winrm/config '@{MaxTimeoutms="1800000"}' 2>$null
winrm set winrm/config/winrs '@{MaxMemoryPerShellMB="800"}' 2>$null
winrm set winrm/config/service '@{AllowUnencrypted="true"}' 2>$null
winrm set winrm/config/service/auth '@{Basic="true"}' 2>$null
winrm set winrm/config/client/auth '@{Basic="true"}' 2>$null

# Ensure HTTP listener exists on port 5985
$listener = winrm enumerate winrm/config/listener 2>$null
if ($listener -notmatch "Transport = HTTP") {
    Log "Creating HTTP listener..."
    winrm create winrm/config/listener?Address=*+Transport=HTTP '@{Port="5985"}' 2>$null
} else {
    winrm set winrm/config/listener?Address=*+Transport=HTTP '@{Port="5985"}' 2>$null
}

Log "WinRM service configured."

# ---------------------------------------------------------------------------
# 4. Firewall rules
# ---------------------------------------------------------------------------
Log "Configuring firewall rules..."
netsh advfirewall firewall set rule name="Windows Remote Management (HTTP-In)" new enable=yes action=allow remoteip=any 2>$null
netsh advfirewall firewall set rule group="Windows Remote Management" new enable=yes 2>$null
# Fallback: create rule if it doesn't exist
netsh advfirewall firewall add rule name="WinRM HTTP" dir=in action=allow protocol=tcp localport=5985 2>$null

Log "Firewall configured."

# ---------------------------------------------------------------------------
# 5. Restart WinRM to pick up all changes
# ---------------------------------------------------------------------------
Log "Restarting WinRM service..."
Restart-Service winrm -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# Verify
$svc = Get-Service winrm -ErrorAction SilentlyContinue
Log "WinRM service status: $($svc.Status)"

# ---------------------------------------------------------------------------
# 6. Create scheduled task (idempotent) — re-applies config at every boot
#    and every 2 minutes for the first 30 minutes after boot.
#    This handles the case where Windows Security resets config after login.
# ---------------------------------------------------------------------------
Log "Creating CAPE-WinRM-Configure scheduled task..."

# Copy this script to a persistent location (floppy may not always be A:\)
$persistPath = "C:\Windows\Temp\cape-configure-winrm.ps1"
if (Test-Path "A:\cape-configure-winrm.ps1") {
    Copy-Item "A:\cape-configure-winrm.ps1" $persistPath -Force
} elseif ($MyInvocation.MyCommand.Path -and (Test-Path $MyInvocation.MyCommand.Path)) {
    Copy-Item $MyInvocation.MyCommand.Path $persistPath -Force
}

# Remove existing task if present (idempotent)
schtasks /delete /tn "CAPE-WinRM-Configure" /f 2>$null

# Create task that runs at system startup + repeats every 2 min for 30 min
schtasks /create /tn "CAPE-WinRM-Configure" `
    /tr "powershell.exe -ExecutionPolicy Bypass -File C:\Windows\Temp\cape-configure-winrm.ps1" `
    /sc ONSTART /ru SYSTEM /rl HIGHEST /f `
    /ri 2 /du 0:30

Log "Scheduled task created."

# ---------------------------------------------------------------------------
# 7. Also create a logon-triggered task as extra safety net
# ---------------------------------------------------------------------------
schtasks /delete /tn "CAPE-WinRM-Logon" /f 2>$null
schtasks /create /tn "CAPE-WinRM-Logon" `
    /tr "powershell.exe -ExecutionPolicy Bypass -File C:\Windows\Temp\cape-configure-winrm.ps1" `
    /sc ONLOGON /ru SYSTEM /rl HIGHEST /f

Log "Logon task created."
Log "=== cape-configure-winrm.ps1 complete ==="
