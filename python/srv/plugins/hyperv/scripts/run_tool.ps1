# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Norvitex OU

param(
    [string]$Tool, [string[]]$ToolArgs, [hashtable]$ToolEnv, [securestring]$SharePassword,
    [securestring]$Key, [securestring]$SecretKey, [securestring]$SourceKey, [securestring]$SourceSecretKey
)

function ConvertTo-PlainText([securestring]$Value) {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($Value)
    try { [Runtime.InteropServices.Marshal]::PtrToStringUni($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($pointer) }
}

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$names = @()
$share = $null
$account = $null
if ($ToolEnv) {
    $names = @($ToolEnv.Keys | Where-Object { $_ -notlike 'ABR_SMB_*' })
    $share = [string]$ToolEnv['ABR_SMB_SHARE']
    $account = [string]$ToolEnv['ABR_SMB_USER']
}

if ($share) {
    if (-not $account -or -not $SharePassword) {
        "Error: $env:COMPUTERNAME cannot open the repository share ${share}: no account to open it with"
        'ABR_TOOL_EXIT=1'
        return
    }
    if (-not ($account.Contains('\') -or $account.Contains('@'))) {
        $account = (($share.TrimStart('\') -split '\\')[0]) + '\' + $account
    }
    if (-not ('AbrSmbConnection' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security;

public static class AbrSmbConnection {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    class NetResource {
        public int Scope, Type = 1, DisplayType, Usage;
        public string LocalName, RemoteName, Comment, Provider;
    }

    [DllImport("mpr.dll", CharSet = CharSet.Unicode, EntryPoint = "WNetAddConnection2W")]
    static extern int WNetAddConnection2(NetResource resource, IntPtr password, string user, int flags);

    [DllImport("mpr.dll", CharSet = CharSet.Unicode, EntryPoint = "WNetCancelConnection2W")]
    static extern int WNetCancelConnection2(string name, int flags, bool force);

    public static void Add(string share, string user, SecureString password) {
        IntPtr secret = Marshal.SecureStringToGlobalAllocUnicode(password);
        try {
            int error = WNetAddConnection2(new NetResource { RemoteName = share }, secret, user, 0);
            if (error != 0) throw new Win32Exception(error);
        } finally {
            Marshal.ZeroFreeGlobalAllocUnicode(secret);
        }
    }

    public static void Remove(string share) {
        WNetCancelConnection2(share, 0, true);
    }
}
'@
    }
    try {
        [AbrSmbConnection]::Add($share, $account, $SharePassword)
    } catch {
        "Error: $env:COMPUTERNAME cannot open the repository share $share as ${account}: $($_.Exception.GetBaseException().Message.Trim())"
        'ABR_TOOL_EXIT=1'
        return
    }
}

$secrets = [ordered]@{ 'key' = $Key; 'secret-key' = $SecretKey; 'source-key' = $SourceKey; 'source-secret-key' = $SourceSecretKey }
$stdin = @($secrets.GetEnumerator() | Where-Object { $_.Value } | ForEach-Object { '{0}={1}' -f $_.Key, (ConvertTo-PlainText $_.Value) })
foreach ($name in $names) { Set-Item -Path "Env:$name" -Value ([string]$ToolEnv[$name]) }
$savedEncoding = $global:OutputEncoding
try {
    if ($stdin.Count) {
        $global:OutputEncoding = New-Object Text.UTF8Encoding $false
        ($stdin + '') | & $Tool @ToolArgs 2>&1 | ForEach-Object { "$_" }
    } else {
        & $Tool @ToolArgs 2>&1 | ForEach-Object { "$_" }
    }
    "ABR_TOOL_EXIT=$LASTEXITCODE"
} finally {
    $global:OutputEncoding = $savedEncoding
    $stdin = $null
    foreach ($name in $names) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
    if ($share) { [AbrSmbConnection]::Remove($share) }
}
