# ZendeskScrapper — セットアップスクリプト
# PowerShell で実行: .\setup.ps1

Set-StrictMode -Off
$ErrorActionPreference = "Stop"

Write-Host "`n=== ZendeskScrapper セットアップ ===`n" -ForegroundColor Cyan

# Python バージョン確認
$pythonCmd = $null
foreach ($cmd in @("python", "python3")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -ge 10) {
                $pythonCmd = $cmd
                Write-Host "[OK] $ver" -ForegroundColor Green
                break
            }
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Host "[ERROR] Python 3.10 以上が必要です。https://python.org からインストールしてください。" -ForegroundColor Red
    exit 1
}

# 仮想環境を作成
if (-not (Test-Path ".venv")) {
    Write-Host "`n[1/3] 仮想環境を作成中..." -ForegroundColor Yellow
    & $pythonCmd -m venv .venv
    Write-Host "      → .venv を作成しました" -ForegroundColor Green
} else {
    Write-Host "`n[1/3] 仮想環境はすでに存在します (.venv)" -ForegroundColor Green
}

# 仮想環境の pip パス
$pip = ".\.venv\Scripts\pip.exe"
$python = ".\.venv\Scripts\python.exe"

# パッケージをインストール
Write-Host "`n[2/3] パッケージをインストール中..." -ForegroundColor Yellow
& $pip install --upgrade pip --quiet
& $pip install -r requirements.txt --quiet
Write-Host "      → playwright, httpx, flask をインストールしました" -ForegroundColor Green

# Playwright ブラウザをインストール
Write-Host "`n[3/3] Playwright Chromium をインストール中..." -ForegroundColor Yellow
& $python -m playwright install chromium
Write-Host "      → Chromium をインストールしました" -ForegroundColor Green

Write-Host @"

=== セットアップ完了 ===

使い方:

  1. チケットをダウンロードする（初回・更新時）:
       .\.venv\Scripts\python.exe scraper.py

     初回はブラウザが開くのでZendeskにログインしてください。
     ログイン後、自動的にダウンロードが始まります。

  2. 検索Webアプリを起動する:
       .\.venv\Scripts\python.exe webapp.py

     ブラウザで http://127.0.0.1:5000 を開いてください。

データ保存先: .\data\tickets\<チケット番号>\

"@ -ForegroundColor Cyan
