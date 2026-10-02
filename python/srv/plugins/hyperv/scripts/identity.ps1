$cluster = ''
if (Get-Command Get-Cluster -ErrorAction SilentlyContinue) {
    try { $cluster = [string](Get-Cluster -ErrorAction SilentlyContinue).Name } catch { }
}

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    cluster = $cluster
    version = [string](Get-CimInstance Win32_OperatingSystem).Caption
}
