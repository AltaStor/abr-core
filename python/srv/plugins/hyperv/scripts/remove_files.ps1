$failed = @()
foreach ($path in @($AbrArgs.paths)) {
    if (-not $path) { continue }
    try {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction Stop }
    } catch {
        $failed += $path
    }
}
[pscustomobject]@{ failed = $failed }
