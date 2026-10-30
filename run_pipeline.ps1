# Run from any directory; output paths remain relative to your current directory.
# Examples:
#   .\run_pipeline.ps1 --demo
#   .\run_pipeline.ps1 --zh --demo
# Setup on another Windows machine:
#   uv venv --python 3.12 .venv
#   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project .venv is missing. See setup commands at the top of run_pipeline.ps1.'
}
$forwarded = @($args)
$pipeline = 'csv_to_pdf_pipeline.py'
if ($forwarded.Count -gt 0 -and $forwarded[0] -eq '--zh') {
    $pipeline = 'csv_to_pdf_pipeline_zh.py'
    $forwarded = @($forwarded | Select-Object -Skip 1)
}
$settings = @{
    PYTHONUTF8 = '1'
    PYTHONIOENCODING = 'utf-8'
    PYTHONPATH = $null
    MPLBACKEND = 'Agg'
    MPLCONFIGDIR = (Join-Path $PSScriptRoot '.tools\matplotlib')
    SE_CACHE_PATH = (Join-Path $PSScriptRoot '.tools\selenium')
    SE_AVOID_STATS = 'true'
    SE_OFFLINE = 'false'
}
$saved = @{}
foreach ($name in $settings.Keys) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
}
try {
    & $python (Join-Path $PSScriptRoot $pipeline) @forwarded
    $pipelineExitCode = $LASTEXITCODE
}
finally {
    foreach ($name in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process')
    }
}
exit $pipelineExitCode
