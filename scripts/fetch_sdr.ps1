# Phase 0.1 — download all 32 FAA SDR year files (1995-2026).
# Idempotent: skips files already present with >1MB. Logs bytes+rows per file.
$ErrorActionPreference = "Continue"
$out = "C:\Users\Sarvan\Desktop\Avionics Workstation\data\sdr"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$log = Join-Path $out "_download_log.csv"
"file,bytes,rows,status" | Out-File $log -Encoding utf8
$ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

foreach ($y in 1995..2026) {
    $f = "SDR-$y.csv"
    $dest = Join-Path $out $f
    $url = "https://external.apic4e.faa.gov/sdrs/retrieve/$f"
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -gt 1MB)) {
        $rows = 0
        try { $rows = [System.IO.File]::ReadLines($dest).GetEnumerator() | Measure-Object | Select-Object -ExpandProperty Count } catch {}
        "$f,$((Get-Item $dest).Length),$($rows-1),cached" | Add-Content $log
        continue
    }
    $ok = $false
    foreach ($try in 1..3) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $dest -UserAgent $ua -TimeoutSec 600 -UseBasicParsing
            if ((Get-Item $dest).Length -gt 100KB) { $ok = $true; break }
        } catch { Start-Sleep -Seconds (5 * $try) }
    }
    if ($ok) {
        $rows = 0
        try { $rows = [System.IO.File]::ReadLines($dest).GetEnumerator() | Measure-Object | Select-Object -ExpandProperty Count } catch {}
        "$f,$((Get-Item $dest).Length),$($rows-1),ok" | Add-Content $log
    } else {
        "$f,0,0,FAILED" | Add-Content $log
    }
}
"DONE $(Get-Date -Format o)" | Add-Content $log
