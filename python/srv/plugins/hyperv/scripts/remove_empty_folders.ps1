$kept = @()
foreach ($folder in @($AbrArgs.folders)) {
    if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { continue }
    if (@(Get-ChildItem -LiteralPath $folder -Force).Count -gt 0) {
        $kept += $folder
        continue
    }
    try {
        Remove-Item -LiteralPath $folder -Force -ErrorAction Stop
    } catch {
        $kept += $folder
    }
}
[pscustomobject]@{ kept = $kept }
