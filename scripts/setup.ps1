param([switch]$CheckOnly, [switch]$Install, [switch]$SkipReferences, [switch]$References, [switch]$Quyet, [switch]$AllowNetwork, [switch]$AllowModelDownload)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { $Args = @('-3', (Join-Path $Root 'scripts/setup.py')) }
else { $Python = Get-Command python -ErrorAction SilentlyContinue; if (-not $Python) { throw 'Python 3.10+ is required' }; $Args = @((Join-Path $Root 'scripts/setup.py')) }
if ($CheckOnly) { $Args += '--check-only' } else { $Args += '--install' }
if ($SkipReferences -or (-not $Install -and -not $References)) { $Args += '--skip-references' }
if ($References) { $Args += '--references' }
if ($Quyet) { $Args += '--quyet' }
if ($AllowNetwork) { $Args += '--allow-network' }
if ($AllowModelDownload) { $Args += '--allow-model-download' }
& $Python.Source @Args
exit $LASTEXITCODE
