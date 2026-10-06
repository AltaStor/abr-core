$created = @()
foreach ($folder in @($AbrArgs.folders)) {
    if (-not $folder) { continue }
    $missing = @()
    $probe = $folder
    while ($probe -and -not (Test-Path -LiteralPath $probe)) {
        $missing += $probe
        $probe = Split-Path -Path $probe -Parent
    }
    $null = New-Item -ItemType Directory -Path $folder -Force
    foreach ($path in $missing) { if ($created -notcontains $path) { $created += $path } }
}
[pscustomobject]@{
    existing = @(@($AbrArgs.files) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
    created = @($created)
}
