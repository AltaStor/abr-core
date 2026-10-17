# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

Convert-VHD -Path $AbrArgs.source -DestinationPath $AbrArgs.destination -VHDType $AbrArgs.type
Remove-Item -LiteralPath $AbrArgs.source -Force
[pscustomobject]@{ path = $AbrArgs.destination }
