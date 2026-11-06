# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$vm = Get-AbrVm $AbrArgs.vm_id

function Get-AbrSettings($vm) {
    $p = Get-VMProcessor -VM $vm
    $m = Get-VMMemory -VM $vm
    [ordered]@{
        'processor count' = [string]$p.Count
        'processor reserve' = "$([int]$p.Reserve)%"
        'processor limit' = "$([int]$p.Maximum)%"
        'processor weight' = [string]$p.RelativeWeight
        'nested virtualization' = $(if ($p.ExposeVirtualizationExtensions) { 'on' } else { 'off' })
        'memory' = "$([int64]($m.Startup / 1MB)) MB"
        'dynamic memory' = $(if ($m.DynamicMemoryEnabled) { "on, $([int64]($m.Minimum / 1MB))-$([int64]($m.Maximum / 1MB)) MB, buffer $($m.Buffer)%" } else { 'off' })
        'memory weight' = [string]$m.Priority
        'notes' = [string]$vm.Notes
        'automatic start' = "$($vm.AutomaticStartAction), delay $($vm.AutomaticStartDelay) s"
        'automatic stop' = [string]$vm.AutomaticStopAction
        'checkpoint type' = [string]$vm.CheckpointType
    }
}

$before = Get-AbrSettings $vm

if ([int]$AbrArgs.num_cpu -gt 0) { Set-VMProcessor -VM $vm -Count ([int64]$AbrArgs.num_cpu) }
$processor = @{}
if ($null -ne $AbrArgs.cpu_reserve) { $processor.Reserve = [int64]$AbrArgs.cpu_reserve }
if ($null -ne $AbrArgs.cpu_limit -and [int64]$AbrArgs.cpu_limit -gt 0) { $processor.Maximum = [int64]$AbrArgs.cpu_limit }
if ($null -ne $AbrArgs.cpu_weight -and [int]$AbrArgs.cpu_weight -gt 0) { $processor.RelativeWeight = [int32]$AbrArgs.cpu_weight }
if ($null -ne $AbrArgs.nested_virtualization) { $processor.ExposeVirtualizationExtensions = [bool]$AbrArgs.nested_virtualization }
if ($processor.Count) { Set-VMProcessor -VM $vm @processor -ErrorAction Stop }

if ([int64]$AbrArgs.memory_mb -gt 0) {
    $memory = [int64]$AbrArgs.memory_mb * 1MB
    if ($AbrArgs.dynamic_memory) {
        $min = [int64]$AbrArgs.memory_minimum_mb * 1MB
        $max = [int64]$AbrArgs.memory_maximum_mb * 1MB
        if ($min -le 0 -or $min -gt $memory) { $min = $memory }
        if ($max -lt $memory) { $max = $memory }
        $dynamic = @{ DynamicMemoryEnabled = $true; StartupBytes = $memory; MinimumBytes = $min; MaximumBytes = $max }
        if ($null -ne $AbrArgs.memory_buffer -and [int]$AbrArgs.memory_buffer -gt 0) { $dynamic.Buffer = [int32]$AbrArgs.memory_buffer }
        Set-VMMemory -VM $vm @dynamic
    } else {
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $false -StartupBytes $memory
    }
}
if ($null -ne $AbrArgs.memory_priority) { Set-VMMemory -VM $vm -Priority ([int32]$AbrArgs.memory_priority) -ErrorAction Stop }

$settings = @{}
if ($null -ne $AbrArgs.notes) { $settings.Notes = [string]$AbrArgs.notes }
if ($AbrArgs.automatic_start_action) { $settings.AutomaticStartAction = [string]$AbrArgs.automatic_start_action }
if ($null -ne $AbrArgs.automatic_start_delay) { $settings.AutomaticStartDelay = [int32]$AbrArgs.automatic_start_delay }
if ($AbrArgs.automatic_stop_action) { $settings.AutomaticStopAction = [string]$AbrArgs.automatic_stop_action }
if ($AbrArgs.checkpoint_type) { $settings.CheckpointType = [string]$AbrArgs.checkpoint_type }
if ($settings.Count) { Set-VM -VM $vm @settings -ErrorAction Stop }

$after = Get-AbrSettings (Get-AbrVm $AbrArgs.vm_id)
$changed = @(foreach ($name in $before.Keys) {
    if ($before[$name] -ne $after[$name]) { [pscustomobject]@{ setting = $name; before = $before[$name]; after = $after[$name] } }
})
[pscustomobject]@{ vm_id = $vm.Id.ToString(); changed = $changed }
