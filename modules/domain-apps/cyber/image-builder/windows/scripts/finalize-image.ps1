# Return control to Packer before stopping the WinRM transport it is using.
$ErrorActionPreference = "Stop"
$workerPath = "C:\cape\finalize-shutdown.ps1"
@'
$ErrorActionPreference = "Stop"
# These bootstrap tasks would otherwise re-enable management on the final VM.
foreach ($name in @("CAPE-WinRM-Configure", "CAPE-WinRM-Logon", "CAPE-FinalizeImage")) {
  Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
}
Remove-Item "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinRM" -Recurse -Force -ErrorAction SilentlyContinue
& "A:\disable-winrm.ps1"
Stop-Computer -Force
'@ | Set-Content -Path $workerPath -Encoding UTF8
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File $workerPath"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(60)
Register-ScheduledTask -TaskName "CAPE-FinalizeImage" -Action $action -Trigger $trigger `
  -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Output "Final WinRM cleanup and shutdown scheduled outside the Packer session."
