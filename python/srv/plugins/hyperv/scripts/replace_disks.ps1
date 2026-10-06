# Point each drive at another file; report what it pointed at before, so the
# caller can put it back.
$vm = Get-AbrVm $AbrArgs.vm_id
foreach ($disk in @($AbrArgs.disks)) {
    $type = [string]$disk.controller_type
    $number = [int]$disk.controller_number
    $location = [int]$disk.controller_location
    if ($type -eq 'SCSI') {
        while (@(Get-VMScsiController -VM $vm).Count -le $number) { Add-VMScsiController -VM $vm }
    }
    $drive = Get-VMHardDiskDrive -VM $vm -ControllerType $type -ControllerNumber $number -ControllerLocation $location
    $previous = ''
    if ($drive) {
        $previous = [string]$drive.Path
        if ($disk.path) {
            Set-VMHardDiskDrive -VMHardDiskDrive $drive -Path $disk.path
        } else {
            Remove-VMHardDiskDrive -VMHardDiskDrive $drive
        }
    } elseif ($disk.path) {
        Add-VMHardDiskDrive -VM $vm -ControllerType $type -ControllerNumber $number -ControllerLocation $location -Path $disk.path
    }
    [pscustomobject]@{
        slot = "$type$($number):$location"
        controller_type = $type; controller_number = $number; controller_location = $location
        previous_path = $previous; path = [string]$disk.path
    }
}
