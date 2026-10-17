# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id
foreach ($drive in @(Get-VMHardDiskDrive -VM $vm)) {
    Get-AbrDriveInfo $drive
}
