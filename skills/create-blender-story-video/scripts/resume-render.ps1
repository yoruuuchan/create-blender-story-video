[CmdletBinding()]
param(
    [string]$ProjectRoot = ".",
    [string]$Python = "python",
    [string]$Blender = "blender",
    [switch]$RebootResume,
    [switch]$EnableAutoResume,
    [switch]$NoFactoryStartup,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AdditionalArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Supervisor = Join-Path $ScriptDir "render_supervisor.py"
$Arguments = @(
    $Supervisor,
    "--project-root", (Resolve-Path $ProjectRoot).Path,
    "--blender", $Blender
)
if ($RebootResume) { $Arguments += "--reboot-resume" }
if ($EnableAutoResume) { $Arguments += "--enable-auto-resume" }
if ($NoFactoryStartup) { $Arguments += "--no-factory-startup" }
if ($AdditionalArgs) { $Arguments += $AdditionalArgs }

& $Python @Arguments
exit $LASTEXITCODE
