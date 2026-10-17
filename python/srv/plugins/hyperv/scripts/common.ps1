# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

$ErrorActionPreference = 'Stop'
$AbrNs = 'root\virtualization\v2'

# Hyper-V WMI methods answer 0 when done, 4096 when they started a job.
function Wait-AbrWmiJob($Result, [string]$What) {
    if ($Result.ReturnValue -eq 0) { return $null }
    if ($Result.ReturnValue -ne 4096) {
        throw "$What failed with return code $($Result.ReturnValue)"
    }
    $job = [WMI]$Result.Job
    while ($job.JobState -eq 3 -or $job.JobState -eq 4) {
        Start-Sleep -Milliseconds 500
        $job.Get()
    }
    if ($job.JobState -ne 7) {
        $detail = [string]$job.ErrorDescription
        if (-not $detail) { $detail = "job state $($job.JobState), error code $($job.ErrorCode)" }
        throw "$What failed: $detail"
    }
    return $job
}

function Get-AbrGuid([string]$Value, [string]$What) {
    $parsed = [guid]::Empty
    if (-not [guid]::TryParse($Value, [ref]$parsed)) { throw "$What '$Value' is not a GUID" }
    return $parsed.ToString()
}

function Get-AbrVm([string]$Id) {
    $vm = Get-VM -Id (Get-AbrGuid $Id 'VM id') -ErrorAction SilentlyContinue
    if (-not $vm) { throw "ABR_VM_NOT_FOUND: virtual machine $Id is not on $env:COMPUTERNAME" }
    return $vm
}

function Get-AbrRecoveryCheckpoint($Vm, [string]$Id) {
    $wanted = Get-AbrGuid $Id 'checkpoint id'
    Get-VMSnapshot -VM $Vm -SnapshotType Recovery -ErrorAction SilentlyContinue |
        Where-Object { $_.Id.ToString() -eq $wanted } | Select-Object -First 1
}

# The same checkpoint read from Hyper-V's WMI provider rather than through
# Get-VMSnapshot. Its InstanceID is 'Microsoft:<checkpoint id>'.
function Get-AbrRecoveryCheckpointSettings([string]$Id) {
    $wanted = Get-AbrGuid $Id 'checkpoint id'
    Get-WmiObject -Namespace $AbrNs -Query (
        "SELECT * FROM Msvm_VirtualSystemSettingData WHERE InstanceID='Microsoft:$wanted'") |
        Where-Object { [string]$_.VirtualSystemType -eq 'Microsoft:Hyper-V:Snapshot:Recovery' } |
        Select-Object -First 1
}

# Every checkpoint of a VM, of any type, as WMI has them.
function Get-AbrVmCheckpointSettings($Vm) {
    @(Get-WmiObject -Namespace $AbrNs -Query (
        "SELECT * FROM Msvm_VirtualSystemSettingData WHERE VirtualSystemIdentifier='$($Vm.Id)'") |
        Where-Object { [string]$_.VirtualSystemType -like 'Microsoft:Hyper-V:Snapshot:*' })
}

# 'Recovery 1b4e28ba-2fa1-11d2-883f-0016d3cca427' for an error message.
function Format-AbrCheckpointSettings($Settings) {
    "$(([string]$Settings.VirtualSystemType) -replace '^Microsoft:Hyper-V:Snapshot:', '') " +
        "$(([string]$Settings.InstanceID) -replace '^Microsoft:', '')"
}

function ConvertTo-AbrWql([string]$Value) {
    return $Value.Replace('\', '\\').Replace("'", "\'")
}

function Get-AbrDriveSlot($Drive) {
    return "$($Drive.ControllerType)$($Drive.ControllerNumber):$($Drive.ControllerLocation)"
}

# The differencing chain under a disk, from the file itself down to its base.
function Get-AbrDiskChain([string]$Path) {
    $chain = @()
    $seen = @{}
    $current = $Path
    while ($current) {
        $key = $current.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "The disk chain of $Path loops back on $current" }
        $seen[$key] = $true
        $vhd = Get-VHD -Path $current
        $chain += [pscustomobject]@{
            path = [string]$vhd.Path
            format = [string]$vhd.VhdFormat
            type = [string]$vhd.VhdType
            size = [int64]$vhd.Size
            file_size = [int64]$vhd.FileSize
            block_size = [int64]$vhd.BlockSize
            logical_sector_size = [int]$vhd.LogicalSectorSize
            physical_sector_size = [int]$vhd.PhysicalSectorSize
            disk_identifier = [string]$vhd.DiskIdentifier
            parent_path = [string]$vhd.ParentPath
        }
        $current = [string]$vhd.ParentPath
    }
    return ,$chain
}

# What a backup needs to know about one hard disk drive of a VM or checkpoint.
function Get-AbrDriveInfo($Drive) {
    $slot = Get-AbrDriveSlot $Drive
    $path = [string]$Drive.Path
    $info = [ordered]@{
        slot = $slot
        controller_type = [string]$Drive.ControllerType
        controller_number = [int]$Drive.ControllerNumber
        controller_location = [int]$Drive.ControllerLocation
        path = $path
        supported = $false
        unsupported_reason = ''
        chain = @()
    }
    $extension = ''
    if ($path) { $extension = [System.IO.Path]::GetExtension($path).ToLowerInvariant() }
    if ($null -ne $Drive.DiskNumber -or -not $path -or
        @('.vhdx', '.avhdx', '.vhd', '.avhd', '.vhds') -notcontains $extension) {
        $info.unsupported_reason = 'pass-through physical disk'
        return [pscustomobject]$info
    }
    if ($extension -eq '.vhds') {
        $info.unsupported_reason = 'shared VHD Set (.vhds)'
        return [pscustomobject]$info
    }
    try {
        $info.chain = Get-AbrDiskChain $path
    } catch {
        $info.unsupported_reason = "cannot read the disk chain: $($_.Exception.Message)"
        return [pscustomobject]$info
    }
    $info.supported = $true
    return [pscustomobject]$info
}
