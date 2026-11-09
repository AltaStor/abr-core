# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id
$current = @(Get-VMNetworkAdapter -VM $vm)
$wanted = @($AbrArgs.adapters)
$found = @{}
$claimed = @{}

function Get-AbrAdapterId($adapter) { ([string]$adapter.Id -replace '^.*\\', '').ToUpperInvariant() }
function Get-AbrMac($value) { ([string]$value -replace '[^0-9A-Fa-f]', '').ToUpperInvariant() }
function Use-AbrAdapter([int]$index, $adapter) { $found[$index] = $adapter; $claimed[[string]$adapter.Id] = $true }
function Get-AbrFree { @($current | Where-Object { -not $claimed[[string]$_.Id] }) }

for ($i = 0; $i -lt $wanted.Count; $i++) {
    $id = ([string]$wanted[$i].adapter_id).ToUpperInvariant()
    if (-not $id) { continue }
    $hit = @(Get-AbrFree | Where-Object { (Get-AbrAdapterId $_) -eq $id })
    if ($hit.Count -eq 1) { Use-AbrAdapter $i $hit[0] }
}
function Test-AbrSameAdapter($adapter, $item) {
    if ([string]$adapter.Name -ne [string]$item.label -or [string]$adapter.SwitchName -ne [string]$item.switch) { return $false }
    $mac = Get-AbrMac $item.mac
    if (-not $item.dynamic_mac -and $mac -and $mac -ne '000000000000' -and (Get-AbrMac $adapter.MacAddress) -ne $mac) { return $false }
    $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -ErrorAction SilentlyContinue
    if ([string]$item.vlan_mode -eq 'Access') { return $vlan.OperationMode -eq 'Access' -and [int]$vlan.AccessVlanId -eq [int]$item.vlan_id }
    if ([string]$item.vlan_mode -eq 'Untagged') { return [string]$vlan.OperationMode -ne 'Access' }
    return $true
}
for ($i = 0; $i -lt $wanted.Count; $i++) {
    if (-not $wanted[$i].adapter_id -or $found.ContainsKey($i)) { continue }
    $hit = @(Get-AbrFree | Where-Object { Test-AbrSameAdapter $_ $wanted[$i] })
    if ($hit.Count -ge 1) { Use-AbrAdapter $i $hit[0] }
}
for ($i = 0; $i -lt $wanted.Count; $i++) {
    if ($wanted[$i].adapter_id -or $found.ContainsKey($i)) { continue }
    $mac = Get-AbrMac $wanted[$i].mac
    if (-not $mac -or $mac -eq '000000000000') { continue }
    $hit = @(Get-AbrFree | Where-Object { (Get-AbrMac $_.MacAddress) -eq $mac })
    if ($hit.Count -eq 1) { Use-AbrAdapter $i $hit[0] }
}
for ($i = 0; $i -lt $wanted.Count; $i++) {
    if ($wanted[$i].adapter_id -or $found.ContainsKey($i)) { continue }
    $label = [string]$wanted[$i].label
    $free = Get-AbrFree
    if ($i -lt $current.Count -and -not $claimed[[string]$current[$i].Id] -and $current[$i].Name -eq $label) {
        Use-AbrAdapter $i $current[$i]
        continue
    }
    $named = @($free | Where-Object { $_.Name -eq $label })
    if ($named.Count -eq 1) { Use-AbrAdapter $i $named[0] }
}

foreach ($extra in @(Get-AbrFree)) {
    $row = [pscustomobject]@{
        label = [string]$extra.Name; status = 'removed'; previous = [string]$extra.SwitchName; switch = ''
        mac = [string]$extra.MacAddress; vlan_changed = $false; previous_vlan = ''
    }
    Remove-VMNetworkAdapter -VMNetworkAdapter $extra -ErrorAction Stop
    $row
}

for ($i = 0; $i -lt $wanted.Count; $i++) {
    $item = $wanted[$i]
    $label = [string]$item.label
    $switch = [string]$item.switch
    $adapter = $found[$i]
    $status = 'unchanged'
    $previous = ''
    if (-not $adapter) {
        $add = @{ VM = $vm; Name = $(if ($label) { $label } else { 'Network Adapter' }); Passthru = $true; ErrorAction = 'Stop' }
        if ($switch) { $add.SwitchName = $switch }
        if ($item.legacy) { $add.IsLegacy = $true }
        $mac = Get-AbrMac $item.mac
        if (-not $item.dynamic_mac -and $mac -and $mac -ne '000000000000') { $add.StaticMacAddress = $mac }
        $adapter = Add-VMNetworkAdapter @add
        $status = 'created'
    } else {
        $previous = [string]$adapter.SwitchName
        if ($previous -ne $switch) {
            if ($switch) { Connect-VMNetworkAdapter -VMNetworkAdapter $adapter -SwitchName $switch -ErrorAction Stop }
            else { Disconnect-VMNetworkAdapter -VMNetworkAdapter $adapter -ErrorAction Stop }
            $status = 'reconnected'
        }
    }
    $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -ErrorAction SilentlyContinue
    $previousVlan = if ($vlan.OperationMode -eq 'Access') { "VLAN $($vlan.AccessVlanId)" } else { [string]$vlan.OperationMode }
    $vlanChanged = $false
    if ([string]$item.vlan_mode -eq 'Access' -and ($vlan.OperationMode -ne 'Access' -or [int]$vlan.AccessVlanId -ne [int]$item.vlan_id)) {
        Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Access -VlanId ([int]$item.vlan_id) -ErrorAction Stop
        $vlanChanged = $status -ne 'created'
    } elseif ([string]$item.vlan_mode -eq 'Untagged' -and $vlan.OperationMode -ne 'Untagged') {
        Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Untagged -ErrorAction Stop
        $vlanChanged = $status -ne 'created'
    }
    [pscustomobject]@{
        label = $label
        status = $status
        previous = $previous
        switch = $switch
        mac = $(if ($status -eq 'created') { [string]$adapter.MacAddress } else { '' })
        vlan_changed = $vlanChanged
        previous_vlan = $previousVlan
    }
}
