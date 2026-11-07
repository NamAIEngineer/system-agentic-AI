param([switch]$Json, [switch]$Strict)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { $Args = @('-3', (Join-Path $Root 'scripts/doctor.py')) }
else { $Python = Get-Command python -ErrorAction SilentlyContinue; if (-not $Python) { throw 'Python 3.10+ is required' }; $Args = @((Join-Path $Root 'scripts/doctor.py')) }
if ($Json) { $Args += '--json' }
if ($Strict) { $Args += '--strict' }
& $Python.Source @Args
exit $LASTEXITCODE
