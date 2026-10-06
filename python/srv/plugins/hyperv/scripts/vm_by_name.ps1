@(Get-VM -Name $AbrArgs.name -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ vm_id = $_.Id.ToString(); name = $_.Name; computer = $env:COMPUTERNAME }
})
