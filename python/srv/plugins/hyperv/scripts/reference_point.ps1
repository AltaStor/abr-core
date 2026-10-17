# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

# Turns the backup's recovery checkpoint into a reference point: Hyper-V merges
# the checkpoint back into the VM's disks and keeps the resilient change
# tracking position it marked, which the next incremental backup reads from.
$vm = Get-AbrVm $AbrArgs.vm_id
$checkpoint = Get-AbrRecoveryCheckpoint $vm $AbrArgs.checkpoint_id
if (-not $checkpoint) {
    throw "ABR_CHECKPOINT_NOT_FOUND: recovery checkpoint $($AbrArgs.checkpoint_id) is not on VM $($vm.Name)"
}
$system = Get-WmiObject -Namespace $AbrNs -Class Msvm_ComputerSystem -Filter "Name='$($vm.Id)'"
$before = @(Get-WmiObject -Namespace $AbrNs -Query (
    "ASSOCIATORS OF {$($system.__PATH)} WHERE ResultClass=Msvm_VirtualSystemReferencePoint") |
    ForEach-Object { [string]$_.InstanceID })
$snapshot = Get-WmiObject -Namespace $AbrNs -Query (
    "SELECT * FROM Msvm_VirtualSystemSettingData WHERE InstanceID='Microsoft:" +
    (ConvertTo-AbrWql $checkpoint.Id.ToString()) + "'")
if (-not $snapshot) { throw "Hyper-V has no settings for recovery checkpoint $($checkpoint.Id)" }
$service = Get-WmiObject -Namespace $AbrNs -Class Msvm_VirtualSystemSnapshotService
$in = $service.GetMethodParameters('ConvertToReferencePoint')
$in.AffectedSnapshot = $snapshot.__PATH
$null = Wait-AbrWmiJob ($service.InvokeMethod('ConvertToReferencePoint', $in, $null)) 'Turning the recovery checkpoint into a reference point'
$created = Get-WmiObject -Namespace $AbrNs -Query (
    "ASSOCIATORS OF {$($system.__PATH)} WHERE ResultClass=Msvm_VirtualSystemReferencePoint") |
    Where-Object { $before -notcontains [string]$_.InstanceID } | Select-Object -First 1
[pscustomobject]@{
    reference_point_id = [string]$created.InstanceID
    rct_ids = @($created.ResilientChangeTrackingIdentifiers | ForEach-Object { [string]$_ })
    computer = $env:COMPUTERNAME
}
