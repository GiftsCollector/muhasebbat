$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root "open-app.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop) {
    $desktop = Join-Path $env:USERPROFILE "Desktop"
}
$lnkPath = Join-Path $desktop "Mohasabat.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnkPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 1
$shortcut.Save()
Write-Host "Shortcut saved:"
Write-Host $lnkPath
