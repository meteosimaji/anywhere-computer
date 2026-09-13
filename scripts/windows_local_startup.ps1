# Register the existing local launcher for the current user's interactive login.
# This does not configure HTTP, install a service, or store credentials.
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Status', 'Remove')]
    [string]$Action = 'Status',
    [string]$Launcher = (Join-Path $env:USERPROFILE '.anywhere-computer\bin\anywhere.cmd')
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This script requires Windows.'
}
$Launcher = [IO.Path]::GetFullPath($Launcher)
if ($Launcher -match '[%"\r\n]' -or -not [IO.Path]::IsPathRooted($Launcher)) {
    throw 'The launcher path cannot contain percent signs, quotes, or newlines.'
}
$startup = [Environment]::GetFolderPath('Startup')
if (-not $startup) { throw 'The current user Startup folder is unavailable.' }
$shortcutPath = Join-Path $startup 'Anywhere Computer Local.lnk'
$command = Join-Path ([Environment]::GetFolderPath('System')) 'cmd.exe'
$arguments = '/d /s /c ""' + $Launcher + '" start"'
$shell = New-Object -ComObject WScript.Shell
try {
    $exists = Test-Path -LiteralPath $shortcutPath
    $shortcut = $shell.CreateShortcut($shortcutPath)
    try {
        $matches = $exists -and
            [string]::Equals($shortcut.TargetPath, $command, [StringComparison]::OrdinalIgnoreCase) -and
            $shortcut.Arguments -ceq $arguments
        if ($Action -ne 'Status' -and $exists -and -not $matches) {
            throw 'An unrelated or differently configured shortcut exists; it was left unchanged.'
        }
        if ($Action -eq 'Install') {
            if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
                throw 'Install the Anywhere Computer local launcher first.'
            }
            if ([IO.Path]::GetExtension($Launcher) -ine '.cmd') {
                throw 'The local launcher must be a .cmd file.'
            }
            $shortcut.TargetPath = $command
            $shortcut.Arguments = $arguments
            $shortcut.WorkingDirectory = Split-Path -Parent $Launcher
            $shortcut.WindowStyle = 7
            $shortcut.Description = 'Start Anywhere Computer local engine after interactive login'
            $shortcut.Save()
        } elseif ($Action -eq 'Remove' -and $exists) {
            Remove-Item -LiteralPath $shortcutPath
        }
    } finally {
        [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    }
    $exists = Test-Path -LiteralPath $shortcutPath
    $verified = $false
    if ($exists) {
        $saved = $shell.CreateShortcut($shortcutPath)
        try {
            $verified = [string]::Equals($saved.TargetPath, $command, [StringComparison]::OrdinalIgnoreCase) -and
                $saved.Arguments -ceq $arguments
        } finally {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($saved)
        }
    }
    if ($Action -eq 'Install' -and -not $verified) { throw 'Saved shortcut verification failed.' }
    [ordered]@{
        action = $Action; installed = $exists; launcher_matches = $verified
        shortcut = $shortcutPath; launcher = $Launcher
        requires_interactive_login = $true; engine_started_by_this_script = $false
    } | ConvertTo-Json
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
