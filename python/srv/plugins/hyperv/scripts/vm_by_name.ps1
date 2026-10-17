# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

@(Get-VM -Name $AbrArgs.name -ErrorAction SilentlyContinue | ForEach-Object {
    [pscustomobject]@{ vm_id = $_.Id.ToString(); name = $_.Name; computer = $env:COMPUTERNAME }
})
