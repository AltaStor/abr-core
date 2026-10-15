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

if (-not $share) {
    foreach ($name in $names) { Set-Item -Path "Env:$name" -Value ([string]$ToolEnv[$name]) }
    try {
        & $Tool @ToolArgs 2>&1 | ForEach-Object { "$_" }
        "ABR_TOOL_EXIT=$LASTEXITCODE"
    } finally {
        foreach ($name in $names) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
    }
    return
}

$account = [string]$ToolEnv['ABR_SMB_USER']
if (-not $account) {
    "Error: $env:COMPUTERNAME cannot open the repository share ${share}: no account to open it with"
    'ABR_TOOL_EXIT=1'
    return
}

$domain = ($share.TrimStart('\') -split '\\')[0]
$user = $account
if ($account.Contains('\')) { $domain, $user = $account.Split('\', 2) }
elseif ($account.Contains('@')) { $domain = $null }

if (-not ('AbrNetOnlyProcess' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections;
using System.Runtime.InteropServices;
using System.Text;

public static class AbrNetOnlyProcess {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }

    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcessWithLogonW(string user, string domain, string password, int logonFlags,
        string application, StringBuilder commandLine, int creationFlags, IntPtr environment, string directory,
        ref STARTUPINFO startupInfo, out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);

    const int LOGON_NETCREDENTIALS_ONLY = 2;
    const int CREATE_UNICODE_ENVIRONMENT = 0x400;
    const int CREATE_NO_WINDOW = 0x08000000;

    public static int Start(string user, string domain, string password, string commandLine, IDictionary environment) {
        StringBuilder block = new StringBuilder();
        ArrayList names = new ArrayList(environment.Keys);
        names.Sort(StringComparer.OrdinalIgnoreCase);
        foreach (object name in names) {
            block.Append(name).Append('=').Append(environment[name]).Append('\0');
        }
        block.Append('\0');
        IntPtr env = Marshal.StringToHGlobalUni(block.ToString());
        try {
            STARTUPINFO startupInfo = new STARTUPINFO();
            startupInfo.cb = Marshal.SizeOf(startupInfo);
            PROCESS_INFORMATION process;
            if (!CreateProcessWithLogonW(user, domain, password, LOGON_NETCREDENTIALS_ONLY, null,
                    new StringBuilder(commandLine), CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, env, null,
                    ref startupInfo, out process)) {
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            CloseHandle(process.hThread);
            CloseHandle(process.hProcess);
            return process.dwProcessId;
        } finally {
            Marshal.FreeHGlobal(env);
        }
    }
}
'@
}

$runId = [guid]::NewGuid().ToString('N')
$outFile = Join-Path $env:SystemRoot "Temp\abr-tool-$runId.log"
$exitFile = Join-Path $env:SystemRoot "Temp\abr-tool-$runId.exit"
$runnerFile = Join-Path $env:SystemRoot "Temp\abr-tool-$runId.ps1"
$runner = @'
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
function Say([string]$line) { [System.IO.File]::AppendAllText($env:ABR_RUN_OUT, $line + "`n", [System.Text.Encoding]::UTF8) }
$code = 1
try {
    try {
        $null = [System.IO.Directory]::GetFileSystemEntries($env:ABR_RUN_SHARE)
    } catch {
        Say "Error: $env:COMPUTERNAME cannot open the repository share $($env:ABR_RUN_SHARE) as $($env:ABR_RUN_ACCOUNT): $($_.Exception.GetBaseException().Message.Trim())"
        return
    }
    $toolArgs = @($env:ABR_RUN_ARGS | ConvertFrom-Json)
    & $env:ABR_RUN_TOOL @toolArgs 2>&1 | ForEach-Object { Say "$_" }
    $code = $LASTEXITCODE
} finally {
    [System.IO.File]::WriteAllText($env:ABR_RUN_EXIT, [string]$code)
}
'@

$environment = [System.Collections.Generic.Dictionary[string, string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($entry in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
    $environment[[string]$entry.Key] = [string]$entry.Value
}
foreach ($name in $names) { $environment[$name] = [string]$ToolEnv[$name] }
$environment['ABR_RUN_TOOL'] = $Tool
$environment['ABR_RUN_ARGS'] = ConvertTo-Json -InputObject @($ToolArgs) -Compress
$environment['ABR_RUN_OUT'] = $outFile
$environment['ABR_RUN_EXIT'] = $exitFile
$environment['ABR_RUN_SHARE'] = $share
$environment['ABR_RUN_ACCOUNT'] = $account
[System.IO.File]::WriteAllText($runnerFile, $runner, [System.Text.Encoding]::UTF8)
$powershell = Join-Path $PSHOME 'powershell.exe'
$commandLine = "`"$powershell`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runnerFile`""

$processId = $null
$reader = $null
try {
    try {
        $processId = [AbrNetOnlyProcess]::Start($user, $domain, [string]$ToolEnv['ABR_SMB_PASSWORD'], $commandLine, $environment)
    } catch {
        "Error: $env:COMPUTERNAME cannot open the repository share $share as ${account}: $($_.Exception.Message)"
        'ABR_TOOL_EXIT=1'
        return
    }
    $pending = ''
    $offset = 0
    while ($true) {
        $finished = Test-Path -LiteralPath $exitFile
        if (Test-Path -LiteralPath $outFile) {
            $stream = [System.IO.File]::Open($outFile, 'Open', 'Read', 'ReadWrite')
            try {
                $null = $stream.Seek($offset, 'Begin')
                $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)
                $chunk = $reader.ReadToEnd()
                $offset = $stream.Position
            } finally {
                if ($reader) { $reader.Dispose(); $reader = $null } else { $stream.Dispose() }
            }
            $pending += $chunk
            $lines = $pending -split "`n"
            $pending = $lines[-1]
            if ($lines.Count -gt 1) {
                foreach ($line in $lines[0..($lines.Count - 2)]) { $line.TrimEnd("`r") }
            }
        }
        if ($finished) {
            if ($pending) { $pending.TrimEnd("`r") }
            break
        }
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-Path -LiteralPath $exitFile)) {
                "Error: abr-vhdx-tool on $env:COMPUTERNAME ended without reporting how"
                'ABR_TOOL_EXIT=1'
                return
            }
            continue
        }
        Start-Sleep -Milliseconds 500
    }
    "ABR_TOOL_EXIT=$((Get-Content -LiteralPath $exitFile -Raw).Trim())"
} finally {
    if ($processId -and (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        & taskkill.exe /T /F /PID $processId 2>&1 | Out-Null
    }
    Remove-Item -LiteralPath $outFile, $exitFile, $runnerFile -Force -ErrorAction SilentlyContinue
}
