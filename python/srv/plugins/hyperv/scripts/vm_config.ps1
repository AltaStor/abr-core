# Everything a restore needs to build the VM again: hardware, firmware,
# network and disk layout. The disks' content is in the backup itself.
$vm = Get-AbrVm $AbrArgs.vm_id
$memory = Get-VMMemory -VM $vm
$processor = Get-VMProcessor -VM $vm

$firmware = $null
$bios = $null
if ($vm.Generation -ge 2) { $firmware = Get-VMFirmware -VM $vm } else { $bios = Get-VMBios -VM $vm }
$security = $null
try { $security = Get-VMSecurity -VM $vm -ErrorAction SilentlyContinue } catch { }

$guestOs = ''
try {
    $kvp = (Get-CimInstance -Namespace $AbrNs -ClassName Msvm_KvpExchangeComponent `
        -Filter "SystemName='$($vm.Id)'" -ErrorAction SilentlyContinue).GuestIntrinsicExchangeItems
    foreach ($item in $kvp) {
        try {
            $props = @{}
            foreach ($p in ([xml]$item).INSTANCE.PROPERTY) { $props[$p.NAME] = $p.VALUE }
            if ($props['Name'] -eq 'OSName') { $guestOs = [string]$props['Data']; break }
        } catch { }
    }
} catch { }

$bootOrder = @()
if ($firmware) {
    foreach ($entry in @($firmware.BootOrder)) {
        $device = ''
        try {
            if ($entry.BootType -eq 'Drive' -and $entry.Device) {
                $device = "$($entry.Device.ControllerType)$($entry.Device.ControllerNumber):$($entry.Device.ControllerLocation)"
            } elseif ($entry.BootType -eq 'Network' -and $entry.Device) {
                $device = [string]$entry.Device.Name
            } else {
                $device = [string]$entry.FirmwarePath
            }
        } catch { }
        $bootOrder += [pscustomobject]@{ boot_type = [string]$entry.BootType; device = $device }
    }
} elseif ($bios) {
    $bootOrder = @($bios.StartupOrder | ForEach-Object { [pscustomobject]@{ boot_type = [string]$_; device = '' } })
}

$adapters = @(Get-VMNetworkAdapter -VM $vm | ForEach-Object {
    $vlan = $null
    try { $vlan = Get-VMNetworkAdapterVlan -VMNetworkAdapter $_ -ErrorAction SilentlyContinue } catch { }
    [pscustomobject]@{
        label = [string]$_.Name
        network = [string]$_.SwitchName
        connected = [bool]$_.SwitchName
        mac = [string]$_.MacAddress
        dynamic_mac = [bool]$_.DynamicMacAddressEnabled
        legacy = [bool]$_.IsLegacy
        vlan_mode = if ($vlan) { [string]$vlan.OperationMode } else { '' }
        vlan_id = if ($vlan) { [int]$vlan.AccessVlanId } else { 0 }
    }
})

$hardDrives = @(Get-VMHardDiskDrive -VM $vm | ForEach-Object {
    [pscustomobject]@{
        slot = Get-AbrDriveSlot $_
        controller_type = [string]$_.ControllerType
        controller_number = [int]$_.ControllerNumber
        controller_location = [int]$_.ControllerLocation
        path = [string]$_.Path
        pass_through = ($null -ne $_.DiskNumber)
    }
})

$dvdDrives = @(Get-VMDvdDrive -VM $vm | ForEach-Object {
    [pscustomobject]@{
        slot = Get-AbrDriveSlot $_
        controller_type = [string]$_.ControllerType
        controller_number = [int]$_.ControllerNumber
        controller_location = [int]$_.ControllerLocation
        path = [string]$_.Path
    }
})

[pscustomobject]@{
    name = $vm.Name
    instance_uuid = $vm.Id.ToString()
    generation = [int]$vm.Generation
    version = [string]$vm.Version
    guest_os = $guestOs
    notes = [string]$vm.Notes
    power_state = [string]$vm.State
    source_host = $env:COMPUTERNAME
    clustered = [bool]$vm.IsClustered
    config_path = [string]$vm.ConfigurationLocation
    checkpoint_path = [string]$vm.SnapshotFileLocation
    smart_paging_path = [string]$vm.SmartPagingFilePath
    checkpoint_type = [string]$vm.CheckpointType
    automatic_start_action = [string]$vm.AutomaticStartAction
    automatic_start_delay = [int]$vm.AutomaticStartDelay
    automatic_stop_action = [string]$vm.AutomaticStopAction
    num_cpu = [int]$processor.Count
    cpu_reserve = [int]$processor.Reserve
    cpu_limit = [int]$processor.Maximum
    cpu_weight = [int]$processor.RelativeWeight
    nested_virtualization = [bool]$processor.ExposeVirtualizationExtensions
    memory_mb = [int64]($memory.Startup / 1MB)
    dynamic_memory = [bool]$memory.DynamicMemoryEnabled
    memory_minimum_mb = [int64]($memory.Minimum / 1MB)
    memory_maximum_mb = [int64]($memory.Maximum / 1MB)
    memory_buffer = [int]$memory.Buffer
    memory_priority = [int]$memory.Priority
    firmware = if ($vm.Generation -ge 2) { 'efi' } else { 'bios' }
    secure_boot_enabled = if ($firmware) { [string]$firmware.SecureBoot -eq 'On' } else { $null }
    secure_boot_template = if ($firmware) { [string]$firmware.SecureBootTemplate } else { '' }
    boot_order = $bootOrder
    tpm_enabled = if ($security) { [bool]$security.TpmEnabled } else { $false }
    shielded = if ($security) { [bool]$security.Shielded } else { $false }
    scsi_controllers = @(Get-VMScsiController -VM $vm | ForEach-Object { [int]$_.ControllerNumber })
    network_adapters = $adapters
    hard_drives = $hardDrives
    dvd_drives = $dvdDrives
}
