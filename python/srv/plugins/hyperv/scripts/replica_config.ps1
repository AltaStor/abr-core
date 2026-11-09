# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id

function Get-AbrAdapterId($adapter) { ([string]$adapter.Id -replace '^.*\\', '').ToUpperInvariant() }

function Find-AbrAdapter([string]$Id) {
    $wanted = $Id.ToUpperInvariant()
    $hit = @(Get-VMNetworkAdapter -VM $vm | Where-Object { (Get-AbrAdapterId $_) -eq $wanted })
    if ($hit.Count -ne 1) { throw "Network adapter $Id is not on VM $($vm.Name)" }
    return $hit[0]
}

function Set-AbrAdapterVlan($adapter, [int]$VlanId) {
    $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -ErrorAction SilentlyContinue
    if ($VlanId -gt 0) {
        if ($vlan.OperationMode -ne 'Access' -or [int]$vlan.AccessVlanId -ne $VlanId) {
            Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Access -VlanId $VlanId -ErrorAction Stop
        }
    } elseif ($vlan -and [string]$vlan.OperationMode -eq 'Access') {
        Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Untagged -ErrorAction Stop
    }
}

foreach ($id in @($AbrArgs.remove | Where-Object { $_ })) {
    Remove-VMNetworkAdapter -VMNetworkAdapter (Find-AbrAdapter ([string]$id)) -ErrorAction Stop
}

foreach ($item in @($AbrArgs.change | Where-Object { $_ })) {
    $adapter = Find-AbrAdapter ([string]$item.id)
    if ($item.network -and $item.connected) {
        if ([string]$adapter.SwitchName -ne [string]$item.network) {
            Connect-VMNetworkAdapter -VMNetworkAdapter $adapter -SwitchName ([string]$item.network) -ErrorAction Stop
        }
    } elseif ($adapter.SwitchName) {
        Disconnect-VMNetworkAdapter -VMNetworkAdapter $adapter -ErrorAction Stop
    }
    Set-AbrAdapterVlan $adapter ([int]$item.vlan_id)
}

$added = @()
foreach ($item in @($AbrArgs.add | Where-Object { $_ })) {
    $add = @{ VM = $vm; Name = $(if ($item.label) { [string]$item.label } else { 'Network Adapter' }); Passthru = $true; ErrorAction = 'Stop' }
    if ($item.network -and $item.connected) { $add.SwitchName = [string]$item.network }
    if ($item.legacy) { $add.IsLegacy = $true }
    $adapter = Add-VMNetworkAdapter @add
    Set-AbrAdapterVlan $adapter ([int]$item.vlan_id)
    $added += (Get-AbrAdapterId $adapter)
}

if ($AbrArgs.rename) {
    Rename-VM -VM $vm -NewName ([string]$AbrArgs.rename) -ErrorAction Stop
}

[pscustomobject]@{ vm_id = $vm.Id.ToString(); added = $added }
