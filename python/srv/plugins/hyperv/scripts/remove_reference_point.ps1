# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

# Destroys a reference point an earlier backup left; one already gone is not
# an error. The change tracking itself stays on.
$vm = Get-VM -Id (Get-AbrGuid $AbrArgs.vm_id 'VM id') -ErrorAction SilentlyContinue
if (-not $vm) {
    [pscustomobject]@{ removed = $false; reason = 'vm_missing' }
} else {
    $system = Get-WmiObject -Namespace $AbrNs -Class Msvm_ComputerSystem -Filter "Name='$($vm.Id)'"
    $wanted = [string]$AbrArgs.reference_point_id
    $point = Get-WmiObject -Namespace $AbrNs -Query (
        "ASSOCIATORS OF {$($system.__PATH)} WHERE ResultClass=Msvm_VirtualSystemReferencePoint") |
        Where-Object { [string]$_.InstanceID -eq $wanted } | Select-Object -First 1
    if (-not $point) {
        [pscustomobject]@{ removed = $false; reason = 'reference_point_missing' }
    } else {
        $service = Get-WmiObject -Namespace $AbrNs -Class Msvm_VirtualSystemReferencePointService
        $in = $service.GetMethodParameters('DestroyReferencePoint')
        $in.AffectedReferencePoint = $point.__PATH
        $null = Wait-AbrWmiJob ($service.InvokeMethod('DestroyReferencePoint', $in, $null)) 'Removing the reference point'
        [pscustomobject]@{ removed = $true; reason = '' }
    }
}
