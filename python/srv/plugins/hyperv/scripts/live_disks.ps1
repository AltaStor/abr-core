$vm = Get-AbrVm $AbrArgs.vm_id
foreach ($drive in @(Get-VMHardDiskDrive -VM $vm)) {
    Get-AbrDriveInfo $drive
}
