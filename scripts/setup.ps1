param([switch]$CheckOnly, [switch]$Install, [switch]$SkipReferences)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { $Args = @('-3', (Join-Path $Root 'scripts/setup.py')) }
else { $Python = Get-Command python -ErrorAction SilentlyContinue; if (-not $Python) { throw 'Python 3.10+ is required' }; $Args = @((Join-Path $Root 'scripts/setup.py')) }
if ($CheckOnly) { $Args += '--check-only' } else { $Args += '--install' }
if ($SkipReferences -or -not $Install) { $Args += '--skip-references' }
& $Python.Source @Args
exit $LASTEXITCODE
