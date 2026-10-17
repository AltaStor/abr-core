# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id
$type = [string]$AbrArgs.controller_type
$number = [int]$AbrArgs.controller_number
$location = [int]$AbrArgs.controller_location
if ($type -eq 'SCSI') {
    while (@(Get-VMScsiController -VM $vm).Count -le $number) { Add-VMScsiController -VM $vm }
}
# A generation 1 VM comes with a DVD drive at IDE 1:0; a disk that lived there wins.
Get-VMDvdDrive -VM $vm -ControllerNumber $number -ControllerLocation $location -ErrorAction SilentlyContinue |
    Where-Object { [string]$_.ControllerType -eq $type } | Remove-VMDvdDrive
$drive = Get-VMHardDiskDrive -VM $vm -ControllerType $type -ControllerNumber $number -ControllerLocation $location
if ($drive) {
    Set-VMHardDiskDrive -VMHardDiskDrive $drive -Path $AbrArgs.path
} else {
    Add-VMHardDiskDrive -VM $vm -ControllerType $type -ControllerNumber $number -ControllerLocation $location -Path $AbrArgs.path
}
[pscustomobject]@{ slot = "$type$($number):$location"; path = $AbrArgs.path }
