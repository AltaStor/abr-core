# Whether a host bundle folder holds every expected file with its SHA-256.
$folder = [string]$AbrArgs.folder
$mismatched = @()
foreach ($file in @($AbrArgs.expected)) {
    $path = Join-Path $folder ([string]$file.name)
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $mismatched += [string]$file.name; continue }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$file.sha256) { $mismatched += [string]$file.name }
}
[pscustomobject]@{
    exists = [bool](Test-Path -LiteralPath $folder -PathType Container)
    complete = ($mismatched.Count -eq 0)
    mismatched = @($mismatched)
}
