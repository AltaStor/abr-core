# Processor and memory back to what the backup recorded (the VM is off).
$vm = Get-AbrVm $AbrArgs.vm_id
if ([int]$AbrArgs.num_cpu -gt 0) { Set-VMProcessor -VM $vm -Count ([int64]$AbrArgs.num_cpu) }
if ([int64]$AbrArgs.memory_mb -gt 0) {
    $memory = [int64]$AbrArgs.memory_mb * 1MB
    if ($AbrArgs.dynamic_memory) {
        $min = [int64]$AbrArgs.memory_minimum_mb * 1MB
        $max = [int64]$AbrArgs.memory_maximum_mb * 1MB
        if ($min -le 0 -or $min -gt $memory) { $min = $memory }
        if ($max -lt $memory) { $max = $memory }
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $true -StartupBytes $memory -MinimumBytes $min -MaximumBytes $max
    } else {
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $false -StartupBytes $memory
    }
}
[pscustomobject]@{ vm_id = $vm.Id.ToString() }
