# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

# What a restore can use on this node: where VM files can go (volumes with a
# drive letter, Cluster Shared Volumes) and how much room each has, the
# virtual switches adapters can connect to, and the VMs already here.
$vmHost = Get-VMHost
$volumes = @(Get-Volume -ErrorAction SilentlyContinue |
    Where-Object { $_.DriveLetter -and $_.DriveType -eq 'Fixed' } | ForEach-Object {
        [pscustomobject]@{
            name = "$($_.DriveLetter):"; path = "$($_.DriveLetter):\"
            capacity = [int64]$_.Size; free = [int64]$_.SizeRemaining; shared = $false
        }
    })
if (Get-Command Get-ClusterSharedVolume -ErrorAction SilentlyContinue) {
    try {
        $volumes += @(Get-ClusterSharedVolume -ErrorAction Stop | ForEach-Object {
            $info = @($_.SharedVolumeInfo) | Where-Object { $_ -and $_.FriendlyVolumeName } | Select-Object -First 1
            if ($info) {
                [pscustomobject]@{
                    name = $info.FriendlyVolumeName; path = $info.FriendlyVolumeName
                    capacity = [int64]$info.Partition.Size; free = [int64]$info.Partition.FreeSpace; shared = $true
                }
            }
        })
    } catch { }
}
$versions = @()
try { $versions = @(Get-VMHostSupportedVersion -ErrorAction Stop | ForEach-Object { [string]$_.Version }) } catch { }

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    vm_path = [string]$vmHost.VirtualMachinePath
    vhd_path = [string]$vmHost.VirtualHardDiskPath
    volumes = $volumes
    switches = @(Get-VMSwitch -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{ name = $_.Name; type = [string]$_.SwitchType }
    })
    vm_names = @(Get-VM -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    versions = $versions
}
