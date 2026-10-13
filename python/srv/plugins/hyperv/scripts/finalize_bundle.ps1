# Checks a staged host bundle and moves it into place under its SHA-256 name.
# A complete bundle already there (another run put it there first) is kept and
# the staging folder dropped; an incomplete one is replaced.
$staging = [string]$AbrArgs.staging
$folder = [string]$AbrArgs.folder
function Test-AbrBundle([string]$Dir) {
    foreach ($file in @($AbrArgs.expected)) {
        $path = Join-Path $Dir ([string]$file.name)
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$file.sha256) { return $false }
    }
    return $true
}
if (-not (Test-AbrBundle $staging)) {
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    throw "ABR_BUNDLE_MISMATCH: the files uploaded to $staging do not match their SHA-256"
}
if (Test-AbrBundle $folder) {
    Remove-Item -LiteralPath $staging -Recurse -Force
    [pscustomobject]@{ status = 'present'; folder = $folder }
    return
}
if (Test-Path -LiteralPath $folder) {
    $stale = "$folder.stale-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
    Rename-Item -LiteralPath $folder -NewName (Split-Path -Path $stale -Leaf)
    Remove-Item -LiteralPath $stale -Recurse -Force -ErrorAction SilentlyContinue
}
try {
    Move-Item -LiteralPath $staging -Destination $folder
} catch {
    if (-not (Test-AbrBundle $folder)) { throw }
    Remove-Item -LiteralPath $staging -Recurse -Force
    [pscustomobject]@{ status = 'present'; folder = $folder }
    return
}
[pscustomobject]@{ status = 'deployed'; folder = $folder }
