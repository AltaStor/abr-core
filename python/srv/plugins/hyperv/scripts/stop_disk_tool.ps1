# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$paths = @($AbrArgs.paths | ? { $_ } | % { ([string]$_).ToLowerInvariant() })
$stopped = @()
foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name='abr-vhdx-tool.exe'")) {
    $line = ([string]$process.CommandLine).ToLowerInvariant()
    if (-not ($paths | ? { $line.Contains($_) })) { continue }
    try {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
        Wait-Process -Id $process.ProcessId -Timeout 30 -ErrorAction SilentlyContinue
        $stopped += "abr-vhdx-tool (process $($process.ProcessId), started $($process.CreationDate.ToString('s')))"
    } catch {
        if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) { throw }
    }
}
[pscustomobject]@{ stopped = $stopped }
