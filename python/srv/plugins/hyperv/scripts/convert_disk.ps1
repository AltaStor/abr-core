Convert-VHD -Path $AbrArgs.source -DestinationPath $AbrArgs.destination -VHDType $AbrArgs.type
Remove-Item -LiteralPath $AbrArgs.source -Force
[pscustomobject]@{ path = $AbrArgs.destination }
