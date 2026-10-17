# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

# A VM with the hardware the backup recorded and no disks yet; the restore
# attaches them next. Anything that fails part way removes the VM again.
$memory = [int64]$AbrArgs.memory_mb * 1MB
$vm = New-VM -Name $AbrArgs.name -Generation ([int16]$AbrArgs.generation) -MemoryStartupBytes $memory `
    -NoVHD -Path $AbrArgs.path
try {
    Set-VMProcessor -VM $vm -Count ([int64]$AbrArgs.num_cpu)
    if ($AbrArgs.dynamic_memory) {
        $min = [int64]$AbrArgs.memory_minimum_mb * 1MB
        $max = [int64]$AbrArgs.memory_maximum_mb * 1MB
        if ($min -le 0 -or $min -gt $memory) { $min = $memory }
        if ($max -lt $memory) { $max = $memory }
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $true -MinimumBytes $min -MaximumBytes $max
        if ([int]$AbrArgs.memory_buffer -gt 0) { Set-VMMemory -VM $vm -Buffer ([int]$AbrArgs.memory_buffer) }
    }
    if ([int]$AbrArgs.generation -ge 2 -and $null -ne $AbrArgs.secure_boot_enabled) {
        if ($AbrArgs.secure_boot_enabled) {
            if ($AbrArgs.secure_boot_template) {
                Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate $AbrArgs.secure_boot_template
            } else {
                Set-VMFirmware -VM $vm -EnableSecureBoot On
            }
        } else {
            Set-VMFirmware -VM $vm -EnableSecureBoot Off
        }
    }
    if ($AbrArgs.notes) { Set-VM -VM $vm -Notes $AbrArgs.notes }
    while (@(Get-VMScsiController -VM $vm).Count -lt [int]$AbrArgs.scsi_controllers) {
        Add-VMScsiController -VM $vm
    }
    Get-VMNetworkAdapter -VM $vm | Remove-VMNetworkAdapter
    foreach ($adapter in @($AbrArgs.network_adapters)) {
        if ($adapter.switch) {
            $nic = Add-VMNetworkAdapter -VM $vm -Name $adapter.name -SwitchName $adapter.switch -Passthru
        } else {
            $nic = Add-VMNetworkAdapter -VM $vm -Name $adapter.name -Passthru
        }
        if ([int]$adapter.vlan_id -gt 0) {
            Set-VMNetworkAdapterVlan -VMNetworkAdapter $nic -Access -VlanId ([int]$adapter.vlan_id)
        }
    }
    $tpm = $false
    if ($AbrArgs.tpm_enabled -and [int]$AbrArgs.generation -ge 2) {
        Set-VMKeyProtector -VM $vm -NewLocalKeyProtector
        Enable-VMTPM -VM $vm
        $tpm = $true
    }
} catch {
    Remove-VM -VM $vm -Force -ErrorAction SilentlyContinue
    throw
}
[pscustomobject]@{
    vm_id = $vm.Id.ToString()
    name = $vm.Name
    path = [string]$vm.ConfigurationLocation
    version = [string]$vm.Version
    tpm_enabled = $tpm
}
