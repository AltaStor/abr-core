# Creates a staging folder for a host bundle and writes the given text files
# into it, after removing staging folders an interrupted upload left behind.
$staging = [string]$AbrArgs.staging
$root = Split-Path -Path $staging -Parent
$null = New-Item -ItemType Directory -Path $root -Force
Get-ChildItem -LiteralPath $root -Directory -Filter '.staging-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-1) } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
$null = New-Item -ItemType Directory -Path (Join-Path $staging 'scripts') -Force
foreach ($file in @($AbrArgs.files)) {
    [IO.File]::WriteAllBytes((Join-Path $staging ([string]$file.name)), [Convert]::FromBase64String([string]$file.content))
}
[pscustomobject]@{ staging = $staging; written = @($AbrArgs.files).Count }
