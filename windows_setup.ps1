# Preparation only. Does not install KakaoTalk, start the agent, register a task,
# change Aside, or obtain any account/browser credentials. Run as the intended
# interactive Windows user. Supply a real Python 3.10+ installation, not WindowsApps.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [string]$ReviewedSourceDirectory = (Join-Path $PSScriptRoot 'vendor\kakaotalk-mcp'),
    [Parameter(Mandatory = $true)][string]$TokenSourceFile,
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'AfterhoursKakaoAgent')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Protect-LocalPath([string]$Path, [bool]$Directory) {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($Directory) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($sid)
    foreach ($principal in @($sid, (New-Object System.Security.Principal.SecurityIdentifier 'S-1-5-18'))) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $principal, 'FullControl', $inherit, 'None', 'Allow'
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

if (-not [System.IO.Path]::IsPathRooted($PythonPath) -or
    $PythonPath -match '(?i)\\WindowsApps\\' -or
    -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Supply an installed Python executable, not the WindowsApps shim.'
}
# Avoid nested native-argument quotes (Windows PowerShell 5.1 strips them).
& $PythonPath -I -c 'import sys,winreg; sys.exit(0 if sys.version_info >= (3,10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Working Windows Python 3.10+ is required.' }
if (-not (Test-Path -LiteralPath $TokenSourceFile -PathType Leaf)) {
    throw 'Supply a token file provisioned separately by the operator.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'windows_agent.py') -PathType Leaf)) {
    throw 'windows_agent.py must be alongside this setup script.'
}
if (Test-Path -LiteralPath $InstallDirectory) {
    # Never overwrite an existing configuration, token, pending journal or runtime.
    throw 'Install directory already exists; use a new private directory or review an upgrade manually.'
}

# Reviewed kronenz/kakaotalk-mcp revision:
# 7b41dd9da473e7a9e2f4b07ec27bdf3289fcc5fb
# Vendor only these modules, not the broad MCP server or its extra dependencies.
# Hashes are for the source bytes in the reviewed checkout (controller uses CRLF).
$hashes = @{
    '__init__.py' = '1d6d1c4b8b1bdbc0a41433a5f250382245c7295d6aff23bd53fec08ec9cda618'
    'config.py' = '3dc60d7039994511ec69ce3676284a6d410882e8073908694f083b9e24d4e68c'
    'controller.py' = '57a15f387a5b2c79c8922892d07f27f037ab2f94e5eed475f5407da1ecaf8543'
    'parser.py' = '26ea17bea154612fb4019a9762fa26da441a80f133cdcf1df0831dcb7c867a96'
}
$source = Join-Path $ReviewedSourceDirectory 'src\kakao_mcp'
$license = Join-Path $ReviewedSourceDirectory 'LICENSE'
if (-not (Test-Path -LiteralPath $license -PathType Leaf)) {
    throw 'Reviewed upstream LICENSE is required; no installation was performed.'
}
foreach ($name in $hashes.Keys) {
    $file = Join-Path $source $name
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $hashes[$name]) {
        throw 'Reviewed upstream source hash mismatch; no installation was performed.'
    }
}

New-Item -ItemType Directory -Path $InstallDirectory | Out-Null
Protect-LocalPath $InstallDirectory $true
$tokenTarget = Join-Path $InstallDirectory 'token.txt'
Copy-Item -LiteralPath $TokenSourceFile -Destination $tokenTarget
Protect-LocalPath $tokenTarget $false

$venv = Join-Path $InstallDirectory 'venv'
& $PythonPath -I -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$python = Join-Path $venv 'Scripts\python.exe'
# Explicit, pinned Windows dependency. No full upstream MCP package is installed.
& $python -I -m pip --isolated install --disable-pip-version-check --only-binary=:all: 'pywin32==311'
if ($LASTEXITCODE -ne 0) { throw 'Pinned pywin32 installation failed.' }

$package = Join-Path $venv 'Lib\site-packages\kakao_mcp'
New-Item -ItemType Directory -Path $package | Out-Null
foreach ($name in $hashes.Keys) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $package $name)
}
Copy-Item -LiteralPath $license -Destination (Join-Path $package 'LICENSE')
$agentPath = Join-Path $InstallDirectory 'windows_agent.py'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_agent.py') -Destination $agentPath
$configPath = Join-Path $InstallDirectory 'config.json'
$config = @{ allowed_rooms = @(); send_enabled = $false } | ConvertTo-Json
[System.IO.File]::WriteAllText($configPath, $config, (New-Object System.Text.UTF8Encoding $false))
Protect-LocalPath $configPath $false
Write-Host 'Prepared only. Edit config.json with exact allowed room titles; sending defaults OFF.'
Write-Host 'Provision KakaoTalk separately and keep rooms already open in the interactive session.'
Write-Host 'Do not delete journal.json to retry an uncertain send; a full journal needs operator review.'
Write-Host 'After review, launch manually in that session (not a service):'
Write-Host ('& "{0}" -I "{1}" --config "{2}"' -f $python, $agentPath, $configPath)
