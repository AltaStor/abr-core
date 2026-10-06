# The disks as the recovery checkpoint froze them: each drive's file is now the
# read-only parent of the differencing disk the VM writes to.
$vm = Get-AbrVm $AbrArgs.vm_id
$checkpoint = Get-AbrRecoveryCheckpoint $vm $AbrArgs.checkpoint_id
if (-not $checkpoint) {
    $why = 'Hyper-V has no recovery checkpoint by that id'
    if (Get-AbrRecoveryCheckpointSettings $AbrArgs.checkpoint_id) {
        $why = 'Hyper-V has it in WMI, but Get-VMSnapshot does not list it'
    }
    throw "ABR_CHECKPOINT_NOT_FOUND: recovery checkpoint $($AbrArgs.checkpoint_id) is not on VM $($vm.Name): $why"
}
foreach ($drive in @($checkpoint.HardDrives)) {
    Get-AbrDriveInfo $drive
}
