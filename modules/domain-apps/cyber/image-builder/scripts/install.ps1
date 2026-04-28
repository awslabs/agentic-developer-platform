# =============================================================================
# install.ps1 — Windows first-boot: CAPE agent + anti-evasion tuning
# =============================================================================
# Runs at first autologon inside the Windows VM (via autounattend.xml).
# All dependencies (Python installer, CAPE agent, Git portable) are on the
# VirtIO ISO at E:\firstboot\ — no internet required inside the VM.
#
# After completion, shuts down the VM cleanly so the host can finalize the
# qcow2 image.
# =============================================================================

$ErrorActionPreference = "Stop"
$LogFile = "C:\firstboot\install.log"

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $entry = "[$ts] $Message"
    Write-Host $entry
    Add-Content -Path $LogFile -Value $entry
}

try {

    Write-Log "=== CAPE first-boot install starting ==="

    # -----------------------------------------------------------------
    # 1. Install Python 3.11 (silent mode, bundled MSI)
    # -----------------------------------------------------------------
    Write-Log "Installing Python 3.11..."
    $pythonMsi = "E:\firstboot\python-3.11.9-amd64.exe"
    if (Test-Path $pythonMsi) {
        Start-Process -FilePath $pythonMsi -ArgumentList @(
            "/quiet",
            "InstallAllUsers=1",
            "PrependPath=1",
            "Include_pip=1",
            "Include_test=0"
        ) -Wait -NoNewWindow
        Write-Log "Python 3.11 installed."
    } else {
        Write-Log "WARNING: Python installer not found at $pythonMsi. Skipping."
    }

    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    # -----------------------------------------------------------------
    # 2. Install CAPE guest agent
    # -----------------------------------------------------------------
    Write-Log "Installing CAPE guest agent..."
    $agentSrc = "E:\firstboot\cape-agent"
    $agentDst = "C:\cape-agent"

    if (Test-Path $agentSrc) {
        Copy-Item -Path $agentSrc -Destination $agentDst -Recurse -Force
        Write-Log "CAPE agent files copied to $agentDst"

        # Install agent as a scheduled task that runs at boot
        $action = New-ScheduledTaskAction -Execute "python.exe" -Argument "C:\cape-agent\agent.py" -WorkingDirectory $agentDst
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest -LogonType ServiceAccount
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

        Register-ScheduledTask -TaskName "CAPEAgent" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
        Write-Log "CAPE agent scheduled task registered."
    } else {
        Write-Log "WARNING: CAPE agent not found at $agentSrc. Skipping."
    }

    # -----------------------------------------------------------------
    # 3. Anti-evasion tuning
    # -----------------------------------------------------------------
    Write-Log "Applying anti-evasion tuning..."

    # 3a. Create plausible user artifacts
    $docsDir = "C:\Users\cape\Documents"
    $dlDir = "C:\Users\cape\Downloads"
    New-Item -ItemType Directory -Path $docsDir -Force | Out-Null
    New-Item -ItemType Directory -Path $dlDir -Force | Out-Null
    # Empty placeholder files — just need to exist for anti-evasion
    [System.IO.File]::WriteAllText("$docsDir\work.docx", "")
    [System.IO.File]::WriteAllText("$docsDir\meeting-notes.docx", "")
    [System.IO.File]::WriteAllText("$docsDir\budget-2026.xlsx", "")
    [System.IO.File]::WriteAllText("$dlDir\invoice.pdf", "")
    [System.IO.File]::WriteAllText("$dlDir\report-q1.pdf", "")
    Write-Log "Plausible user artifacts created."

    # 3b. Disable Windows Defender (registry-level, belt-and-suspenders with autounattend)
    $defenderKey = "HKLM:\SOFTWARE\Policies\Microsoft\Windows Defender"
    if (-not (Test-Path $defenderKey)) {
        New-Item -Path $defenderKey -Force | Out-Null
    }
    Set-ItemProperty -Path $defenderKey -Name "DisableAntiSpyware" -Value 1 -Type DWord
    Set-ItemProperty -Path $defenderKey -Name "DisableAntiVirus" -Value 1 -Type DWord

    $rtKey = "$defenderKey\Real-Time Protection"
    if (-not (Test-Path $rtKey)) {
        New-Item -Path $rtKey -Force | Out-Null
    }
    Set-ItemProperty -Path $rtKey -Name "DisableRealtimeMonitoring" -Value 1 -Type DWord
    Set-ItemProperty -Path $rtKey -Name "DisableBehaviorMonitoring" -Value 1 -Type DWord
    Write-Log "Windows Defender disabled via registry."

    # 3c. Disable Windows Update
    $wuKey = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    if (-not (Test-Path $wuKey)) {
        New-Item -Path $wuKey -Force | Out-Null
    }
    Set-ItemProperty -Path $wuKey -Name "NoAutoUpdate" -Value 1 -Type DWord
    Stop-Service -Name wuauserv -Force -ErrorAction SilentlyContinue
    Set-Service -Name wuauserv -StartupType Disabled -ErrorAction SilentlyContinue
    Write-Log "Windows Update disabled."

    # 3d. Disable UAC (EnableLUA=0)
    Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Name "EnableLUA" -Value 0 -Type DWord
    Write-Log "UAC disabled."

    # 3e. Disable Action Center notifications
    $acKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer"
    if (-not (Test-Path $acKey)) {
        New-Item -Path $acKey -Force | Out-Null
    }
    Set-ItemProperty -Path $acKey -Name "HideSCAHealth" -Value 1 -Type DWord
    Write-Log "Action Center notifications disabled."

    # 3f. Disable screensaver + set timeout to never
    $ssKey = "HKCU:\Control Panel\Desktop"
    Set-ItemProperty -Path $ssKey -Name "ScreenSaveActive" -Value "0"
    Set-ItemProperty -Path $ssKey -Name "ScreenSaveTimeOut" -Value "0"
    Write-Log "Screensaver disabled."

    # 3g. Disable sleep + hibernate
    powercfg /change standby-timeout-ac 0
    powercfg /change standby-timeout-dc 0
    powercfg /change monitor-timeout-ac 0
    powercfg /change monitor-timeout-dc 0
    powercfg /hibernate off
    Write-Log "Sleep and hibernate disabled."

    # 3h. Disable Cortana
    $cortanaKey = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Windows Search"
    if (-not (Test-Path $cortanaKey)) {
        New-Item -Path $cortanaKey -Force | Out-Null
    }
    Set-ItemProperty -Path $cortanaKey -Name "AllowCortana" -Value 0 -Type DWord
    Write-Log "Cortana disabled."

    # 3i. Disable OneDrive
    $odKey = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive"
    if (-not (Test-Path $odKey)) {
        New-Item -Path $odKey -Force | Out-Null
    }
    Set-ItemProperty -Path $odKey -Name "DisableFileSyncNGSC" -Value 1 -Type DWord
    Write-Log "OneDrive disabled."

    # 3j. Set realistic hostname (already done in autounattend, verify)
    $hostname = $env:COMPUTERNAME
    Write-Log "Hostname: $hostname (expected: WINVM01)"

    # -----------------------------------------------------------------
    # 4. Clean up
    # -----------------------------------------------------------------
    Write-Log "Cleaning temporary files..."
    Remove-Item -Path "C:\Windows\Temp\*" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "C:\Users\cape\AppData\Local\Temp\*" -Recurse -Force -ErrorAction SilentlyContinue

    Write-Log "=== CAPE first-boot install complete ==="
    Write-Log "Shutting down in 5 seconds..."

} catch {
    Write-Log "ERROR: $_"
    Write-Log "Stack trace: $($_.ScriptStackTrace)"
    # Still shut down on error — the host will check the install log
}

# Shutdown cleanly so the host can finalize the qcow2
shutdown /s /t 5
