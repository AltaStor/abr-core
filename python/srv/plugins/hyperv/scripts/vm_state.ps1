$vm = Get-VM -Id (Get-AbrGuid $AbrArgs.vm_id 'VM id') -ErrorAction SilentlyContinue
if ($vm) {
    # The "Backup (volume shadow copy)" integration service, found by its
    # component id rather than its display name, which is localized.
    $vss = $null
    try {
        $vss = Get-VMIntegrationService -VM $vm -ErrorAction SilentlyContinue |
            Where-Object { [string]$_.Id -match '5CED1297-4598-4915-A5FC-AD21BB4D02A4' } |
            Select-Object -First 1
    } catch { }

    [pscustomobject]@{
        vm_id = $vm.Id.ToString()
        name = $vm.Name
        state = [string]$vm.State
        status = [string]$vm.Status
        operational_status = @($vm.OperationalStatus | ForEach-Object { [string]$_ })
        version = [string]$vm.Version
        generation = [int]$vm.Generation
        computer = $env:COMPUTERNAME
        clustered = [bool]$vm.IsClustered
        checkpoint_type = [string]$vm.CheckpointType
        checkpoint_count = @(Get-VMSnapshot -VM $vm -ErrorAction SilentlyContinue).Count
        backup_service_enabled = if ($vss) { [bool]$vss.Enabled } else { $null }
        backup_service_status = if ($vss) { [string]$vss.PrimaryStatusDescription } else { '' }
    }
}
