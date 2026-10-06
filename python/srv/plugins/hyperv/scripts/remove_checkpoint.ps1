$vm = Get-VM -Id (Get-AbrGuid $AbrArgs.vm_id 'VM id') -ErrorAction SilentlyContinue
if (-not $vm) {
    [pscustomobject]@{ removed = $false; reason = 'vm_missing' }
} else {
    $checkpoint = Get-AbrRecoveryCheckpoint $vm $AbrArgs.checkpoint_id
    # WMI has the checkpoint even when Get-VMSnapshot does not list it.
    $snapshot = Get-AbrRecoveryCheckpointSettings $AbrArgs.checkpoint_id
    if (-not $checkpoint -and -not $snapshot) {
        [pscustomobject]@{ removed = $false; reason = 'checkpoint_missing' }
    } else {
        # Removing a checkpoint merges its differencing disks back; Hyper-V
        # finishes that merge in the background.
        $removed = $false
        if ($checkpoint) {
            try {
                Remove-VMSnapshot -VMSnapshot $checkpoint -Confirm:$false
                $removed = $true
            } catch {
                if (-not $snapshot) { throw $_ }
            }
        }
        if (-not $removed) {
            $service = Get-WmiObject -Namespace $AbrNs -Class Msvm_VirtualSystemSnapshotService
            $in = $service.GetMethodParameters('DestroySnapshot')
            $in.AffectedSnapshot = $snapshot.__PATH
            $null = Wait-AbrWmiJob ($service.InvokeMethod('DestroySnapshot', $in, $null)) 'Removing the recovery checkpoint'
        }
        [pscustomobject]@{ removed = $true; reason = '' }
    }
}
