param([ValidateSet("plan","apply")][string]$Mode = "plan")
# plan  = read-only preview against production (the database itself refuses writes).
# apply = writes the missing journals. Needs a typed YES for the backup. One transaction, verified, rolled back on any difference.
# The database URL is asked with hidden input and is never written to any file.

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

Write-Host ""
Write-Host "=== Backfill ($Mode) ===" -ForegroundColor Cyan

python -c "import psycopg2" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing psycopg2-binary==2.9.9 (same version as requirements.txt)..."
    python -m pip install psycopg2-binary==2.9.9
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed. Send me the error above." -ForegroundColor Red; exit 1 }
}

if ($Mode -eq "apply") {
    Write-Host ""
    Write-Host "This WILL WRITE journal entries to the production database." -ForegroundColor Yellow
    Write-Host "Did you take a full backup of the Render database just now?"
    $answer = Read-Host "Type YES (capital letters) to continue"
    if ($answer -cne "YES") { Write-Host "Cancelled. Nothing was written." -ForegroundColor Red; exit 1 }
}

Write-Host ""
Write-Host "Paste the External Database URL from Render, then press Enter."
Write-Host "(Nothing is shown while you paste. This is normal.)"
$secure = Read-Host "Database URL" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$url = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
if ([string]::IsNullOrWhiteSpace($url)) { Write-Host "No URL entered. Nothing was run." -ForegroundColor Red; exit 1 }

$env:SECRET_KEY = "backfill"
$env:PYTHONIOENCODING = "utf-8"
$env:SQLALCHEMY_SILENCE_UBER_WARNING = "1"
$env:DATABASE_URL = $url.Trim()
if ($Mode -eq "plan") {
    $env:PGOPTIONS = "-c default_transaction_read_only=on"
    $out = "backfill_plan.txt"
    $cmdline = "python scripts\backfill_document_journals.py > $out 2>&1"
} else {
    Remove-Item Env:PGOPTIONS -ErrorAction SilentlyContinue
    $out = "backfill_result.txt"
    $cmdline = "python scripts\backfill_document_journals.py --apply --i-have-a-backup > $out 2>&1"
}

Write-Host ""
Write-Host "Running... this can take a few minutes. Please wait."
try {
    cmd /c $cmdline
} finally {
    Remove-Item Env:DATABASE_URL, Env:PGOPTIONS, Env:SECRET_KEY -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Done. Report saved to: $out" -ForegroundColor Green
Write-Host "Now tell Claude: done."
