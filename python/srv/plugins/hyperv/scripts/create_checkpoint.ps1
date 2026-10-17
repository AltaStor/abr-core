# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

# A recovery checkpoint (snapshot type 32768) is the one Hyper-V makes for
# backup applications: it freezes the disks like any checkpoint, can be turned
# into an RCT reference point afterwards, and stays out of the user's
# checkpoint tree.
$vm = Get-AbrVm $AbrArgs.vm_id
$system = Get-WmiObject -Namespace $AbrNs -Class Msvm_ComputerSystem -Filter "Name='$($vm.Id)'"
$service = Get-WmiObject -Namespace $AbrNs -Class Msvm_VirtualSystemSnapshotService
$settings = ([WMIClass]"\\.\root\virtualization\v2:Msvm_VirtualSystemSnapshotSettingData").CreateInstance()
# 1 = application consistent (VSS in the guest), 2 = crash consistent
$settings.ConsistencyLevel = [uint16]$AbrArgs.consistency_level
$settings.IgnoreNonSnapshottableDisks = $true

# The new checkpoint is looked for in WMI, where it is created. Get-VMSnapshot,
# asked in the same session that listed the checkpoints beforehand, has
# answered without it.
$before = @(Get-AbrVmCheckpointSettings $vm | ForEach-Object { [string]$_.InstanceID })

$in = $service.GetMethodParameters('CreateSnapshot')
$in.AffectedSystem = $system.__PATH
$in.SnapshotSettings = $settings.GetText([System.Management.TextFormat]::WmiDtd20)
$in.SnapshotType = [uint16]32768
$out = $service.InvokeMethod('CreateSnapshot', $in, $null)
$job = Wait-AbrWmiJob $out 'Creating the recovery checkpoint'

# What Hyper-V says it made: the method's output when it finished at once, the
# job's affected elements when it ran as a job. Failing those, the one recovery
# checkpoint of the VM that was not there before.
function Find-AbrCreatedCheckpoint {
    $made = @()
    if ($out.ResultingSnapshot) {
        try { $made += [WMI]$out.ResultingSnapshot } catch { }
    }
    if ($job) {
        try { $made += @($job.GetRelated('Msvm_VirtualSystemSettingData')) } catch { }
    }
    $made = @($made | Where-Object { [string]$_.VirtualSystemType -like 'Microsoft:Hyper-V:Snapshot:*' })
    if ($made) { return $made[0] }
    Get-AbrVmCheckpointSettings $vm |
        Where-Object {
            $before -notcontains [string]$_.InstanceID -and
            [string]$_.VirtualSystemType -eq 'Microsoft:Hyper-V:Snapshot:Recovery'
        } |
        Sort-Object CreationTime -Descending | Select-Object -First 1
}

$checkpoint = $null
for ($attempt = 0; $attempt -lt 10 -and -not $checkpoint; $attempt++) {
    if ($attempt) { Start-Sleep -Seconds 1 }
    $checkpoint = Find-AbrCreatedCheckpoint
}
if (-not $checkpoint) {
    $known = @(Get-AbrVmCheckpointSettings $vm | ForEach-Object { Format-AbrCheckpointSettings $_ })
    $listed = @(Get-VMSnapshot -VM (Get-AbrVm $AbrArgs.vm_id) -ErrorAction SilentlyContinue |
        ForEach-Object { "$($_.SnapshotType) $($_.Id)" })
    throw ('Hyper-V reported the recovery checkpoint as created, but the VM does not have it. ' +
        "Checkpoints of the VM in WMI: $(if ($known) { $known -join ', ' } else { 'none' }); " +
        "listed by Get-VMSnapshot: $(if ($listed) { $listed -join ', ' } else { 'none' })")
}

$id = Get-AbrGuid (([string]$checkpoint.InstanceID) -replace '^Microsoft:', '') 'checkpoint id'
if ([string]$checkpoint.VirtualSystemType -ne 'Microsoft:Hyper-V:Snapshot:Recovery') {
    # Hyper-V made some other kind of checkpoint. It is of no use to a backup,
    # and is this run's own, so it does not stay behind in the user's tree.
    $made = Format-AbrCheckpointSettings $checkpoint
    try {
        $in = $service.GetMethodParameters('DestroySnapshot')
        $in.AffectedSnapshot = $checkpoint.__PATH
        $null = Wait-AbrWmiJob ($service.InvokeMethod('DestroySnapshot', $in, $null)) 'Removing the checkpoint'
        $made += ', removed again'
    } catch {
        $made += ", which could not be removed: $($_.Exception.Message)"
    }
    throw "Hyper-V made another kind of checkpoint than the recovery checkpoint asked for: $made"
}

$created = Get-Date
try { $created = [Management.ManagementDateTimeConverter]::ToDateTime([string]$checkpoint.CreationTime) } catch { }

[pscustomobject]@{
    checkpoint_id = $id
    name = [string]$checkpoint.ElementName
    created_at = $created.ToUniversalTime().ToString('o')
    consistency_level = [int]$AbrArgs.consistency_level
    computer = $env:COMPUTERNAME
}
