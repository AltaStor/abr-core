# Runs abr-vhdx-tool on this host. Everything that varies arrives as a
# parameter, never in the script text, so that S3 credentials and encryption
# keys stay out of PowerShell's script block logging. The S3 keys reach the
# tool as the AWS SDK's environment variables, off its command line.
param([string]$Tool, [string[]]$ToolArgs, [hashtable]$ToolEnv)

# The tool reports failures on stderr; they are its output here, not errors
# that should stop the script.
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$names = @()
if ($ToolEnv) { $names = @($ToolEnv.Keys) }
foreach ($name in $names) { Set-Item -Path "Env:$name" -Value ([string]$ToolEnv[$name]) }
try {
    & $Tool @ToolArgs 2>&1 | ForEach-Object { "$_" }
    "ABR_TOOL_EXIT=$LASTEXITCODE"
} finally {
    foreach ($name in $names) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
}
