# Boot from the disk the VM booted from at backup time.
$vm = Get-AbrVm $AbrArgs.vm_id
if ($vm.Generation -ge 2) {
    if ($AbrArgs.slot -match '^([A-Za-z]+)(\d+):(\d+)$') {
        $drive = Get-VMHardDiskDrive -VM $vm -ControllerType $Matches[1] -ControllerNumber $Matches[2] -ControllerLocation $Matches[3]
        if ($drive) { Set-VMFirmware -VM $vm -FirstBootDevice $drive }
    }
} else {
    $order = @(@($AbrArgs.boot_order) | ForEach-Object { [string]$_.boot_type } |
        Where-Object { @('CD', 'IDE', 'LegacyNetworkAdapter', 'Floppy') -contains $_ })
    if ($order.Count -eq 4) { Set-VMBios -VM $vm -StartupOrder $order }
}
[pscustomobject]@{ generation = [int]$vm.Generation }
