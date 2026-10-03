# download_data.ps1
# Downloads all files from a public Google Drive folder into ./data

$ErrorActionPreference = "Stop"

# Google Drive folder URL
$folderUrl = "https://drive.google.com/drive/folders/19bCT5pKF-QnfW05FW0Eb2dUsMrrnbUSD?usp=sharing"

# Target directory (./data next to this script)
$targetDir = Join-Path $PSScriptRoot "data"

# Create data folder if missing
if (-not (Test-Path $targetDir)) {
    Write-Host "Creating directory: $targetDir" -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
}

# Check Python
Write-Host "Checking Python..." -ForegroundColor Cyan
try {
    $pythonVersion = python --version 2>&1
    Write-Host "Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Error "Python not found. Install Python 3.10+ and add it to PATH."
    exit 1
}

# Check gdown
Write-Host "Checking gdown..." -ForegroundColor Cyan
$gdownInstalled = $false
try {
    $gdownVersion = python -m gdown --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "gdown already installed: $gdownVersion" -ForegroundColor Green
        $gdownInstalled = $true
    }
} catch {
    # not installed
}

if (-not $gdownInstalled) {
    Write-Host "gdown not found. Installing..." -ForegroundColor Yellow
    python -m pip install --upgrade gdown
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install gdown. Check your internet connection and permissions."
        exit 1
    }
    Write-Host "gdown installed successfully." -ForegroundColor Green
}

# Download the folder
Write-Host "Downloading files from Google Drive into $targetDir ..." -ForegroundColor Cyan
python -m gdown --folder $folderUrl -O $targetDir

if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] Download completed. Files are in $targetDir" -ForegroundColor Green
} else {
    Write-Error "[ERROR] Download failed. Exit code: $LASTEXITCODE"
    exit $LASTEXITCODE
}