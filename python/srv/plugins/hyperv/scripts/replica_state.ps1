# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

function Get-AbrReplicaState($Vm) {
    $disks = @(
        Get-VMHardDiskDrive -VM $Vm | ForEach-Object {
            $path = [string]$_.Path
            $size = $null
            $parent = ''
            $lastWrite = ''
            if ($path -and (Test-Path -LiteralPath $path)) {
                $vhd = Get-VHD -Path $path -ErrorAction SilentlyContinue
                if ($vhd) {
                    $size = [int64]$vhd.Size
                    $parent = [string]$vhd.ParentPath
                }
                $lastWrite = (Get-Item -LiteralPath $path).LastWriteTimeUtc.ToString('o')
            }
            [pscustomobject]@{
                slot = Get-AbrDriveSlot $_
                path = $path
                size_bytes = $size
                parent_path = $parent
                last_write_utc = $lastWrite
            }
        }
    )
    $checkpoints = @(
        Get-VMSnapshot -VM $Vm -ErrorAction SilentlyContinue | ForEach-Object {
            [pscustomobject]@{ id = $_.Id.ToString(); name = [string]$_.Name }
        }
    )
    $vmId = $Vm.Id.ToString()
    $lastStart = $null
    $starts = Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Hyper-V-Worker-Admin'; Id = 18500 } -MaxEvents 5000 -ErrorAction SilentlyContinue |
        Where-Object { $_.Properties.Count -ge 2 -and ([string]$_.Properties[1].Value) -eq $vmId } |
        Select-Object -First 1
    if ($starts) { $lastStart = [int64]$starts.RecordId }
    [pscustomobject]@{
        vm_id = $vmId
        last_start_record = $lastStart
        name = $Vm.Name
        state = [string]$Vm.State
        status = [string]$Vm.Status
        computer = $env:COMPUTERNAME
        notes = [string]$Vm.Notes
        configuration_location = [string]$Vm.ConfigurationLocation
        checkpoints = $checkpoints
        disks = $disks
    }
}

if ($AbrArgs.vm_id) {
    $vm = Get-VM -Id (Get-AbrGuid $AbrArgs.vm_id 'VM id') -ErrorAction SilentlyContinue
    if ($vm) { Get-AbrReplicaState $vm }
} elseif ($AbrArgs.marker) {
    $marker = [string]$AbrArgs.marker
    Get-VM | Where-Object { ([string]$_.Notes).Contains($marker) } | ForEach-Object { Get-AbrReplicaState $_ }
} elseif ($AbrArgs.folder) {
    $folder = [string]$AbrArgs.folder
    $tag = [string]$AbrArgs.tag
    if ($tag -and (Test-Path -LiteralPath $folder)) {
        $attached = @{}
        Get-VM | Get-VMHardDiskDrive | ForEach-Object {
            $path = [string]$_.Path
            $seen = 0
            while ($path -and -not $attached.ContainsKey($path.ToLowerInvariant()) -and $seen -lt 64) {
                $attached[$path.ToLowerInvariant()] = $true
                $vhd = Get-VHD -Path $path -ErrorAction SilentlyContinue
                $path = if ($vhd) { [string]$vhd.ParentPath } else { '' }
                $seen++
            }
        }
        $roots = @($folder)
        if ($AbrArgs.subfolders) {
            $roots = @(Get-ChildItem -LiteralPath $folder -Directory -Filter ([string]$AbrArgs.subfolders) -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
        $roots | ForEach-Object { Get-ChildItem -LiteralPath $_ -Recurse -File -ErrorAction SilentlyContinue } |
            Where-Object { $_.Name.Contains($tag) -and @('.vhdx', '.avhdx') -contains $_.Extension.ToLowerInvariant() } |
            ForEach-Object {
                [pscustomobject]@{
                    path = $_.FullName
                    size_bytes = [int64]$_.Length
                    attached = $attached.ContainsKey($_.FullName.ToLowerInvariant())
                }
            }
    }
}
