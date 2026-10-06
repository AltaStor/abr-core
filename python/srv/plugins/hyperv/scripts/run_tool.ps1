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
$share = $null
if ($ToolEnv) {
    # ABR_SMB_* name a local repository's share, which this script opens; the
    # tool never sees them.
    $names = @($ToolEnv.Keys | Where-Object { $_ -notlike 'ABR_SMB_*' })
    $share = [string]$ToolEnv['ABR_SMB_SHARE']
}
if ($share) {
    # A WinRM session has no network credentials of its own (NTLM does not
    # pass them on), so the share is opened with the repository's account,
    # for this session only.
    $mapping = @{ RemotePath = $share; Persistent = $false; ErrorAction = 'Stop' }
    if ($ToolEnv['ABR_SMB_USER']) {
        $mapping.UserName = [string]$ToolEnv['ABR_SMB_USER']
        $mapping.Password = [string]$ToolEnv['ABR_SMB_PASSWORD']
    }
    try {
        $null = New-SmbMapping @mapping
    } catch {
        $who = if ($mapping.UserName) { " as $($mapping.UserName)" } else { '' }
        "Error: $env:COMPUTERNAME cannot open the repository share $share${who}: $($_.Exception.Message)"
        'ABR_TOOL_EXIT=1'
        return
    }
}
foreach ($name in $names) { Set-Item -Path "Env:$name" -Value ([string]$ToolEnv[$name]) }
try {
    & $Tool @ToolArgs 2>&1 | ForEach-Object { "$_" }
    "ABR_TOOL_EXIT=$LASTEXITCODE"
} finally {
    foreach ($name in $names) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
    if ($share) { Remove-SmbMapping -RemotePath $share -Force -Confirm:$false -ErrorAction SilentlyContinue }
}
