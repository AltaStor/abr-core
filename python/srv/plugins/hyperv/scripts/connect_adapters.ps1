$vm = Get-AbrVm $AbrArgs.vm_id
$current = @(Get-VMNetworkAdapter -VM $vm)
$index = 0
foreach ($wanted in @($AbrArgs.adapters)) {
    $label = [string]$wanted.label
    $adapter = $null
    if ($index -lt $current.Count -and $current[$index].Name -eq $label) { $adapter = $current[$index] }
    if (-not $adapter) {
        $named = @($current | Where-Object { $_.Name -eq $label })
        if ($named.Count -eq 1) { $adapter = $named[0] }
    }
    $index++
    if (-not $adapter) {
        [pscustomobject]@{ label = $label; status = 'missing'; previous = ''; switch = [string]$wanted.switch }
        continue
    }
    $previous = [string]$adapter.SwitchName
    $switch = [string]$wanted.switch
    if ($previous -ne $switch) {
        if ($switch) { Connect-VMNetworkAdapter -VMNetworkAdapter $adapter -SwitchName $switch -ErrorAction Stop }
        else { Disconnect-VMNetworkAdapter -VMNetworkAdapter $adapter -ErrorAction Stop }
    }
    $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -ErrorAction SilentlyContinue
    $previousVlan = if ($vlan.OperationMode -eq 'Access') { "VLAN $($vlan.AccessVlanId)" } else { [string]$vlan.OperationMode }
    $vlanChanged = $false
    if ([string]$wanted.vlan_mode -eq 'Access' -and ($vlan.OperationMode -ne 'Access' -or [int]$vlan.AccessVlanId -ne [int]$wanted.vlan_id)) {
        Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Access -VlanId ([int]$wanted.vlan_id) -ErrorAction Stop
        $vlanChanged = $true
    } elseif ([string]$wanted.vlan_mode -eq 'Untagged' -and $vlan.OperationMode -ne 'Untagged') {
        Set-VMNetworkAdapterVlan -VMNetworkAdapter $adapter -Untagged -ErrorAction Stop
        $vlanChanged = $true
    }
    [pscustomobject]@{
        label = $label
        status = if ($previous -ne $switch) { 'reconnected' } else { 'unchanged' }
        previous = $previous
        switch = $switch
        vlan_changed = $vlanChanged
        previous_vlan = $previousVlan
    }
}
