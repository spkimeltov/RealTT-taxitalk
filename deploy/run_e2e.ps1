<#
.SYNOPSIS
    e2e_test.py 를 taxitalk-web 컨테이너 안에서 실행한다.

.DESCRIPTION
    시험 발화 합성에만 기존 tts-api 를 쓰므로, 그 키를 interpreter-web 컨테이너에서
    꺼내 넘긴다. 서비스 자체는 TTS 를 쓰지 않는다.

.EXAMPLE
    ./run_e2e.ps1
    ./run_e2e.ps1 -Mode single -PatientLang ja
#>
[CmdletBinding()]
param(
    [ValidateSet('both', 'single', 'dual')][string]$Mode = 'both',
    [string]$PatientLang = 'en',
    [string]$RemoteDir = '/home/user/taxitalk'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LabRoot = Split-Path -Parent $ProjectRoot
$CredFile = Join-Path $LabRoot 'ELTOV_GPU_SERVER\.env'
if (-not (Test-Path $CredFile)) { throw "접속 정보 파일이 없습니다: $CredFile" }

$cred = @{}
foreach ($line in Get-Content $CredFile) {
    if ($line -match '^\s*(\w+)\s*:\s*(.+?)\s*$') { $cred[$Matches[1]] = $Matches[2] }
}
$Target = "$($cred.id)@$($cred.ip)"

$script = @"
set -e
SUDO() { echo '$($cred.pwd)' | sudo -S -p "" "`$@"; }
KEY=`$(SUDO docker exec interpreter-web printenv TTS_API_KEY | tr -d '\r')
if [ -z "`$KEY" ]; then echo 'TTS_API_KEY 를 찾지 못했습니다'; exit 1; fi
SUDO docker cp $RemoteDir/deploy/e2e_test.py taxitalk-web:/tmp/e2e_test.py
SUDO docker exec -e TTS_API_KEY="`$KEY" -e E2E_MODE=$Mode -e E2E_PATIENT_LANG=$PatientLang \
    taxitalk-web python3 -W ignore /tmp/e2e_test.py
"@ -replace "`r`n", "`n"

$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
$ErrorActionPreference = 'Continue'
& plink -batch -ssh -P $cred.port $Target -pw $cred.pwd "echo $b64 | base64 -d | bash" 2>&1 |
    ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) { throw "e2e 실행 실패 (exit $LASTEXITCODE)" }
