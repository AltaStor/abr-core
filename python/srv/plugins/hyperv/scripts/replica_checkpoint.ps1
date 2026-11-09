# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id
$name = [string]$AbrArgs.name
$timeout = [int]$AbrArgs.timeout_seconds
if ($timeout -le 0) { $timeout = 600 }

function Get-AbrReplicaDiskPaths($Vm) {
    @(Get-VMHardDiskDrive -VM $Vm | ForEach-Object { [string]$_.Path })
}

function Wait-AbrReplicaDisks($Vm, [string]$Extension, [bool]$Wanted, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        $current = Get-VM -Id $Vm.Id
        $paths = Get-AbrReplicaDiskPaths $current
        $matching = @($paths | Where-Object { $_.ToLowerInvariant().EndsWith($Extension) })
        $done = if ($Wanted) { $paths.Count -gt 0 -and $matching.Count -eq $paths.Count } else { $matching.Count -eq 0 }
        if ($done -and -not ([string]$current.Status).ToLowerInvariant().Contains('merg')) { return $paths }
        if ((Get-Date) -gt $deadline) {
            throw "Hyper-V did not finish switching the disks of $($Vm.Name) within $Seconds s: $($paths -join ', '), status $($current.Status)"
        }
        Start-Sleep -Milliseconds 500
    }
}

switch ([string]$AbrArgs.action) {
    'create' {
        if (Get-VMSnapshot -VM $vm -Name $name -ErrorAction SilentlyContinue) { throw "Checkpoint $name already exists on $($vm.Name)" }
        Checkpoint-VM -VM $vm -SnapshotName $name
        $paths = Wait-AbrReplicaDisks $vm '.avhdx' $true 60
        [pscustomobject]@{ action = 'create'; name = $name; disks = $paths }
    }
    'revert' {
        $checkpoint = Get-VMSnapshot -VM $vm -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $checkpoint) { throw "Checkpoint $name is not on $($vm.Name)" }
        Restore-VMSnapshot -VMSnapshot $checkpoint -Confirm:$false
        Remove-VMSnapshot -VMSnapshot $checkpoint
        $paths = Wait-AbrReplicaDisks $vm '.avhdx' $false $timeout
        [pscustomobject]@{ action = 'revert'; name = $name; disks = $paths }
    }
    'remove' {
        $checkpoint = Get-VMSnapshot -VM $vm -Name $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($checkpoint) { Remove-VMSnapshot -VMSnapshot $checkpoint }
        $paths = Wait-AbrReplicaDisks $vm '.avhdx' $false $timeout
        [pscustomobject]@{ action = 'remove'; name = $name; disks = $paths }
    }
    default { throw "Unknown checkpoint action $($AbrArgs.action)" }
}
