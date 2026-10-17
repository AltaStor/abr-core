# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id
$before = [string]$vm.State
if ($AbrArgs.action -eq 'start') {
    if ($vm.State -ne 'Running') { Start-VM -VM $vm }
} elseif ($AbrArgs.action -eq 'turnoff') {
    if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }
}
$vm = Get-VM -Id $vm.Id
[pscustomobject]@{ previous_state = $before; state = [string]$vm.State }
