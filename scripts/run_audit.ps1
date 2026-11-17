# Read-only audit of document journals against the production database.
# - Asks for the database URL (hidden input). The URL is never written to any file.
# - Sets PGOPTIONS so PostgreSQL itself refuses any write (read-only session).
# - Writes the report to audit_result.txt in the project folder.

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

Write-Host ""
Write-Host "=== Read-only audit ===" -ForegroundColor Cyan

# psycopg2 is needed to talk to PostgreSQL (same pinned version as requirements.txt)
python -c "import psycopg2" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing psycopg2-binary==2.9.9 on this computer (same version as requirements.txt)..."
    python -m pip install psycopg2-binary==2.9.9
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed. Send me the error above." -ForegroundColor Red; exit 1 }
}

Write-Host ""
Write-Host "Paste the External Database URL from Render, then press Enter."
Write-Host "(Nothing is shown while you paste. This is normal.)"
$secure = Read-Host "Database URL" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$url = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
if ([string]::IsNullOrWhiteSpace($url)) { Write-Host "No URL entered. Nothing was run." -ForegroundColor Red; exit 1 }

$env:PGOPTIONS = "-c default_transaction_read_only=on"
$env:SECRET_KEY = "audit-only"
$env:PYTHONIOENCODING = "utf-8"
$env:SQLALCHEMY_SILENCE_UBER_WARNING = "1"
$snapshot = Join-Path $project "audit_snapshot.db"
$out = Join-Path $project "audit_result.txt"

Write-Host ""
Write-Host "Step 1/2: copying data from production into a local file (read-only, fast)..."
try {
    $env:DATABASE_URL = $url.Trim()
    cmd /c "python scripts\snapshot_to_sqlite.py audit_snapshot.db 2>&1"
} finally {
    Remove-Item Env:DATABASE_URL, Env:PGOPTIONS -ErrorAction SilentlyContinue
}
if (-not (Test-Path $snapshot)) { Write-Host "Copy failed. Send me the messages above." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "Step 2/2: running the audit on the local copy (production is no longer touched)..."
$env:DATABASE_URL = "sqlite:///" + ($snapshot -replace "\\", "/")
try {
    cmd /c "python scripts\audit_document_journals.py > audit_result.txt 2>&1"
} finally {
    Remove-Item Env:DATABASE_URL, Env:SECRET_KEY -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Done. Report saved to: $out" -ForegroundColor Green
Write-Host "Last line (must say OK):"
Get-Content $out -Tail 1
Write-Host ""
Write-Host "Now tell Claude: done."
