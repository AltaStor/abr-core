$local = @(Get-Volume -ErrorAction SilentlyContinue |
    ? { $_.DriveLetter -and $_.DriveType -eq 'Fixed' } | % {
    [pscustomobject]@{
        name = "$($_.DriveLetter):"; path = "$($_.DriveLetter):\"
        capacity = $_.Size; free = $_.SizeRemaining; shared = $false
    }
})

$dnsName = $env:COMPUTERNAME
try { $dnsName = [System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName } catch { }

$vms = @()
$disks = @()
$nics = @()

foreach ($vm in Get-VM) {
    $os = ''
    try {
        $kvp = (Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_KvpExchangeComponent `
            -Filter "SystemName='$($vm.Id)'" -ErrorAction SilentlyContinue).GuestIntrinsicExchangeItems
        foreach ($item in $kvp) {
            if ($item -match '<PROPERTY NAME="Name" TYPE="string"><VALUE>OSName</VALUE></PROPERTY>.*?<PROPERTY NAME="Data" TYPE="string"><VALUE>(.*?)</VALUE>') {
                $os = $Matches[1]
                break
            }
        }
    } catch { }

    $vms += [pscustomobject]@{
        vm_id = $vm.Id.ToString(); name = $vm.Name; power_state = [string]$vm.State
        num_cpu = [int]$vm.ProcessorCount; memory_mb = [int]($vm.MemoryStartup / 1MB)
        generation = [int]$vm.Generation; guest_os = $os; version = [string]$vm.Version
    }

    foreach ($d in Get-VMHardDiskDrive -VM $vm) {
        $info = $null
        try { $info = Get-VHD -Path $d.Path -ErrorAction SilentlyContinue } catch { }
        $disks += [pscustomobject]@{
            vm_id = $vm.Id.ToString(); path = $d.Path
            name = [System.IO.Path]::GetFileName($d.Path)
            controller = "$($d.ControllerType)$($d.ControllerNumber):$($d.ControllerLocation)"
            capacity_bytes = if ($info) { [int64]$info.Size } else { 0 }
            file_bytes = if ($info) { [int64]$info.FileSize } else { 0 }
            format = if ($info) { [string]$info.VhdFormat } else { '' }
            disk_type = if ($info) { [string]$info.VhdType } else { '' }
            identifier = if ($info) { [string]$info.DiskIdentifier } else { '' }
        }
    }

    foreach ($n in Get-VMNetworkAdapter -VM $vm) {
        $nics += [pscustomobject]@{
            vm_id = $vm.Id.ToString(); id = $n.Id; name = $n.Name
            mac = $n.MacAddress; network = $n.SwitchName; connected = [bool]$n.SwitchName
        }
    }
}

[pscustomobject]@{
    host = [pscustomobject]@{
        name = $env:COMPUTERNAME
        dns_name = $dnsName
        version = (Get-CimInstance Win32_OperatingSystem).Caption
        stores = $local
        networks = @(Get-VMSwitch -ErrorAction SilentlyContinue | % { $_.Name })
    }
    vms = $vms
    disks = $disks
    nics = $nics
}
