# Whether abr-vhdx-tool is installed at the given path, and which build it is.
$path = [string]$AbrArgs.path
$item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
if (-not $item) {
    [pscustomobject]@{ exists = $false; sha256 = ''; version = ''; size = 0 }
} else {
    $version = ''
    try {
        $ErrorActionPreference = 'Continue'
        $version = ((& $path --version 2>&1 | ForEach-Object { "$_" }) -join ' ').Trim()
    } catch { }
    [pscustomobject]@{
        exists = $true
        sha256 = [string](Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        version = $version
        size = [int64]$item.Length
    }
}
