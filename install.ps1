<#
.SYNOPSIS
  Product Idea Radar 자동 설치 스크립트

.DESCRIPTION
  GitHub Releases에서 최신 ProductIdeaRadar.exe 를 내려받아
  로컬에 설치하고 바탕화면 / 시작 메뉴 바로가기를 만든다.
  저장소에는 exe 를 포함하지 않으며, 항상 Release 에셋에서 받는다.

.USAGE
  PowerShell:  powershell -ExecutionPolicy Bypass -File install.ps1
  또는 install.bat 더블클릭
#>

[CmdletBinding()]
param(
    # 특정 태그를 받고 싶을 때 지정 (기본: 최신 릴리스)
    [string]$Tag = "",
    # 설치 경로 (기본: %LOCALAPPDATA%\ProductIdeaRadar)
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ProductIdeaRadar")
)

$ErrorActionPreference = "Stop"

$Owner   = "lsi9923"
$Repo    = "Product-Idea-Radar-2-"
$AssetName = "ProductIdeaRadar.exe"

Write-Host "=== Product Idea Radar 설치 ===" -ForegroundColor Cyan

# 1) 릴리스 메타데이터 조회
if ([string]::IsNullOrWhiteSpace($Tag)) {
    $apiUrl = "https://api.github.com/repos/$Owner/$Repo/releases/latest"
} else {
    $apiUrl = "https://api.github.com/repos/$Owner/$Repo/releases/tags/$Tag"
}

Write-Host "릴리스 정보 확인 중..." -ForegroundColor Gray
try {
    $release = Invoke-RestMethod -Uri $apiUrl -Headers @{ "User-Agent" = "PIR-Installer" }
} catch {
    Write-Error "릴리스 정보를 가져오지 못했습니다. 인터넷 연결 또는 릴리스 존재 여부를 확인하세요.`n$apiUrl"
    exit 1
}

$asset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
if (-not $asset) {
    Write-Error "릴리스 '$($release.tag_name)' 에서 자산 '$AssetName' 을 찾을 수 없습니다."
    exit 1
}

$downloadUrl = $asset.browser_download_url
Write-Host "버전     : $($release.tag_name)" -ForegroundColor Green
Write-Host "다운로드 : $downloadUrl" -ForegroundColor Gray

# 2) 설치 폴더 준비
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
$targetExe = Join-Path $InstallDir $AssetName

# 3) 다운로드
Write-Host "다운로드 중... (약 76MB, 잠시 걸릴 수 있습니다)" -ForegroundColor Yellow
Invoke-WebRequest -Uri $downloadUrl -OutFile $targetExe -Headers @{ "User-Agent" = "PIR-Installer" }

if (-not (Test-Path $targetExe)) {
    Write-Error "다운로드에 실패했습니다."
    exit 1
}
$sizeMB = [math]::Round((Get-Item $targetExe).Length / 1MB, 1)
Write-Host "설치 완료: $targetExe ($sizeMB MB)" -ForegroundColor Green

# 4) 바로가기 생성 (바탕화면 + 시작 메뉴)
$WshShell = New-Object -ComObject WScript.Shell

$desktop = [Environment]::GetFolderPath("Desktop")
$desktopLnk = Join-Path $desktop "제품 아이디어 레이더.lnk"
$sc = $WshShell.CreateShortcut($desktopLnk)
$sc.TargetPath = $targetExe
$sc.WorkingDirectory = $InstallDir
$sc.Description = "Product Idea Radar"
$sc.Save()
Write-Host "바탕화면 바로가기 생성: $desktopLnk" -ForegroundColor Green

$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startLnk = Join-Path $startMenu "제품 아이디어 레이더.lnk"
$sc2 = $WshShell.CreateShortcut($startLnk)
$sc2.TargetPath = $targetExe
$sc2.WorkingDirectory = $InstallDir
$sc2.Description = "Product Idea Radar"
$sc2.Save()
Write-Host "시작 메뉴 바로가기 생성: $startLnk" -ForegroundColor Green

Write-Host ""
Write-Host "설치가 완료되었습니다. 바탕화면의 '제품 아이디어 레이더' 를 실행하세요." -ForegroundColor Cyan

# 5) 바로 실행할지 물어보기 (대화형일 때만)
if ($Host.UI.RawUI -and -not $env:CI) {
    $run = Read-Host "지금 실행할까요? (Y/N)"
    if ($run -match '^[Yy]') {
        Start-Process $targetExe
    }
}
