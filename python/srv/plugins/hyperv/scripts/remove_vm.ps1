# Remove a VM a failed restore had created, with its configuration folder.
$vm = Get-VM -Id (Get-AbrGuid $AbrArgs.vm_id 'VM id') -ErrorAction SilentlyContinue
if ($vm) {
    $folder = [string]$vm.ConfigurationLocation
    if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }
    Remove-VM -VM $vm -Force
    [pscustomobject]@{ removed = $true; configuration_location = $folder }
} else {
    [pscustomobject]@{ removed = $false; configuration_location = '' }
}
