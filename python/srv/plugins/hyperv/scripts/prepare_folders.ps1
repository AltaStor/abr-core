foreach ($folder in @($AbrArgs.folders)) {
    if ($folder) { $null = New-Item -ItemType Directory -Path $folder -Force }
}
[pscustomobject]@{
    existing = @(@($AbrArgs.files) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
}
