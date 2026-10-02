$clustered = [bool](Get-Command Get-ClusterNode -ErrorAction SilentlyContinue)

$cluster = ''
$nodes = @()
$owners = @()
$csv = @()

if ($clustered) {
    $nodes = @(Get-ClusterNode -ErrorAction SilentlyContinue | % {
        $n = $_.Name
        $ips = @()
        try {
            $ips = @([System.Net.Dns]::GetHostAddresses($n) |
                ? { $_.AddressFamily -eq 'InterNetwork' } | % { $_.IPAddressToString })
        } catch { }
        $fqdn = $n
        try { $fqdn = [System.Net.Dns]::GetHostEntry($n).HostName } catch { }
        [pscustomobject]@{ name = $n; state = [string]$_.State; fqdn = $fqdn; addresses = $ips }
    })

    $owners = @(Get-ClusterGroup -ErrorAction SilentlyContinue |
        ? { $_.GroupType -eq 'VirtualMachine' } |
        % { [pscustomobject]@{ name = $_.Name; owner = [string]$_.OwnerNode; state = [string]$_.State } })

    $csv = @(Get-ClusterSharedVolume -ErrorAction SilentlyContinue | % {
        $i = @($_.SharedVolumeInfo) | ? { $_ -and $_.FriendlyVolumeName } | select -First 1
        if ($i) {
            [pscustomobject]@{
                name = $i.FriendlyVolumeName; path = $i.FriendlyVolumeName
                capacity = [int64]$i.Partition.Size; free = [int64]$i.Partition.FreeSpace
                shared = $true
            }
        }
    })

    try { $cluster = [string](Get-Cluster -ErrorAction SilentlyContinue).Name } catch { }
}

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    cluster = $cluster
    nodes = $nodes
    owners = $owners
    shared_stores = $csv
}
