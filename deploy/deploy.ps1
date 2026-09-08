<#
.SYNOPSIS
    TAXI-TALK 를 ELTOV GPU 서버에 배포한다.

.DESCRIPTION
    프로젝트를 tar 로 묶어 SSH(plink) 표준입력으로 전송하고, 서버에서
    docker compose 로 빌드/기동한 뒤 nginx 서브패스를 연결한다.
    접속 정보는 ../../ELTOV_GPU_SERVER/.env 에서 읽는다.

.EXAMPLE
    ./deploy.ps1
    ./deploy.ps1 -SkipBuild -SkipNginx      # 코드만 다시 올리고 재시작
    ./deploy.ps1 -SetEnv 'SCREEN_LAYOUT=single'
#>
[CmdletBinding()]
param(
    [switch]$SkipUpload,
    [switch]$SkipBuild,
    [switch]$SkipNginx,
    [switch]$SkipHealth,
    [string]$RemoteDir = '/home/user/demo/taxitalk',
    # 접속 정보 파일. 비워 두면 프로젝트 옆의 ELTOV_GPU_SERVER\.env 를 쓴다.
    [string]$CredFile,
    # 서버 .env 의 값을 덮어쓴다. 기존 값이 있어도 바꾼다는 점이 `.env.example`
    # 의 신규 항목 추가와 다르다. 예: -SetEnv 'SCREEN_LAYOUT=single'
    [string[]]$SetEnv = @()
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LabRoot = Split-Path -Parent $ProjectRoot
if (-not $CredFile) { $CredFile = Join-Path $LabRoot 'ELTOV_GPU_SERVER\.env' }

if (-not (Test-Path $CredFile)) { throw "접속 정보 파일이 없습니다: $CredFile" }

$cred = @{}
foreach ($line in Get-Content $CredFile) {
    if ($line -match '^\s*(\w+)\s*:\s*(.+?)\s*$') { $cred[$Matches[1]] = $Matches[2] }
}
foreach ($k in 'ip', 'port', 'id', 'pwd') {
    if (-not $cred.ContainsKey($k)) { throw "$CredFile 에 '$k' 항목이 없습니다." }
}

$Target = "$($cred.id)@$($cred.ip)"

function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Script)

    # PowerShell here-string 은 CRLF 로 저장되므로 bash 로 보내기 전에 LF 로 바꾼다.
    $resolved = $Script.Replace('__PW__', $cred.pwd).Replace('__DIR__', $RemoteDir)
    $resolved = $resolved -replace "`r`n", "`n"
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($resolved))
    # docker compose 는 진행 상황을 stderr 로 쓴다. ErrorActionPreference=Stop 이면
    # PowerShell 이 이를 종료 오류로 취급하므로, 이 구간만 완화하고 종료 코드로 판단한다.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & plink -batch -ssh -P $cred.port $Target -pw $cred.pwd "echo $b64 | base64 -d | bash" 2>&1 |
            ForEach-Object { Write-Host $_ }
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) { throw "원격 명령 실패 (exit $LASTEXITCODE)" }
}

$TextExtensions = @(
    '.py', '.js', '.css', '.html', '.yml', '.yaml', '.conf', '.md', '.txt',
    '.example', '.ps1', '.json', '.sh'
)
$TextNames = @('Dockerfile', '.dockerignore', '.gitattributes')

function New-Stage {
    <#
      로컬 파일이 CRLF 로 저장되어 있어도 서버에서는 LF 여야 한다.
      (Dockerfile 의 줄 연속 문자, docker compose 의 env_file 값에 \r 이 섞인다)
      전송 전에 스테이징 디렉터리로 복사하면서 텍스트 파일만 LF 로 바꾼다.
    #>
    $stage = Join-Path $env:TEMP 'taxitalk-stage'
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Path $stage | Out-Null

    # 제외는 프로젝트 루트 기준 상대 경로로 판단한다. 절대 경로로 '*\data\*' 를
    # 거르면 용어집이 든 app\data\ 까지 함께 빠진다.
    #
    # 아카이브를 통째로 base64 문자열로 만들어 보내므로 큰 폴더가 섞이면 메모리에서
    # 터진다. 로컬 개발용 가상환경(.venv 수백 MB)과 저장소 내부(.git)는 서버에서
    # 쓰지 않으니 여기서 잘라낸다.
    foreach ($file in Get-ChildItem -Path $ProjectRoot -Recurse -File -Force) {
        $relative = $file.FullName.Substring($ProjectRoot.Length + 1)
        if ($relative -like 'models\*' -or
            $relative -like 'data\*' -or
            $relative -like '.venv\*' -or
            $relative -like '.git\*' -or
            $relative -like '.idea\*' -or
            $relative -like '*__pycache__*' -or
            $file.Extension -eq '.pyc' -or
            $file.Name -eq '.env') { continue }

        $destination = Join-Path $stage $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        if ($TextExtensions -contains $file.Extension -or $TextNames -contains $file.Name) {
            $text = [IO.File]::ReadAllText($file.FullName) -replace "`r`n", "`n"
            [IO.File]::WriteAllText($destination, $text, [Text.UTF8Encoding]::new($false))
        }
        else {
            Copy-Item -LiteralPath $file.FullName -Destination $destination
        }
    }
    return $stage
}

function Send-Project {
    Write-Host '== 프로젝트 전송 ==' -ForegroundColor Cyan
    $tar = Join-Path $env:TEMP 'taxitalk-deploy.tar.gz'
    $b64 = "$tar.b64"
    if (Test-Path $tar) { Remove-Item $tar }

    $stage = New-Stage
    & tar.exe -czf $tar -C $stage .
    if ($LASTEXITCODE -ne 0) { throw 'tar 생성 실패' }

    [IO.File]::WriteAllText($b64, [Convert]::ToBase64String([IO.File]::ReadAllBytes($tar)))
    $size = [Math]::Round((Get-Item $tar).Length / 1KB, 1)
    Write-Host "  아카이브 ${size}KB"

    $remote = "mkdir -p $RemoteDir && base64 -d | tar -xzf - -C $RemoteDir && echo 전송완료"
    $args = @('-batch', '-ssh', '-P', $cred.port, $Target, '-pw', $cred.pwd, $remote)
    $proc = Start-Process -FilePath 'plink' -ArgumentList $args `
        -RedirectStandardInput $b64 -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) { throw "전송 실패 (exit $($proc.ExitCode))" }
    Remove-Item $tar, $b64 -ErrorAction SilentlyContinue
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}

$EnsureEnv = @'
set -e
cd __DIR__
mkdir -p models data/sessions
if [ ! -f .env ]; then cp .env.example .env; echo ".env 생성"; fi
# .env.example 에 새로 생긴 항목만 이어붙인다(기존에 튜닝한 값은 건드리지 않는다).
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"
  if ! grep -qE "^${key}=" .env; then
    echo "$line" >> .env
    echo "항목 추가: $key"
  fi
done < .env.example
grep -E '^(WHISPER_MODEL|WHISPER_CACHE_DIRS|VLLM_MODEL|MIC_MODE|PATIENT_LANGUAGES)=' .env
'@

# `EnsureEnv` 는 없는 항목만 이어붙이므로 서버에서 이미 굳어진 값은 바뀌지 않는다.
# 기본값을 바꿨을 때처럼 기존 값을 밀어야 하는 경우에만 이쪽을 쓴다.
$ApplyEnv = @'
set -e
cd __DIR__
apply() {
  key="$1"; value="$2"
  if grep -qE "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
    echo "값 변경: ${key}=${value}"
  else
    printf '%s=%s\n' "$key" "$value" >> .env
    echo "항목 추가: ${key}=${value}"
  fi
}
__PAIRS__
'@

$BuildUp = @'
set -e
SUDO() { echo '__PW__' | sudo -S -p "" "$@"; }
cd __DIR__
SUDO docker compose build
SUDO docker compose up -d
SUDO docker compose ps
'@

$Nginx = @'
set -e
SUDO() { echo '__PW__' | sudo -S -p "" "$@"; }
SUDO cp __DIR__/deploy/taxitalk-upstream.conf /etc/nginx/conf.d/taxitalk-upstream.conf
SUDO python3 __DIR__/deploy/patch_nginx.py --snippet __DIR__/deploy/taxitalk-location.conf
SUDO nginx -t
SUDO nginx -s reload
echo "nginx 반영 완료"
'@

$Health = @'
SUDO() { echo '__PW__' | sudo -S -p "" "$@"; }
for i in $(seq 1 72); do
  code=$(curl -s -o /tmp/_taxitalk_health.json -w '%{http_code}' http://127.0.0.1:18093/health || echo 000)
  ok=$(python3 -c "import json;print(json.load(open('/tmp/_taxitalk_health.json')).get('ok'))" 2>/dev/null || echo -)
  echo "[$i] http=$code ok=$ok"
  if [ "$ok" = "True" ]; then break; fi
  sleep 5
done
echo "--- /health ---"
cat /tmp/_taxitalk_health.json; echo
echo "--- /api/config ---"
curl -s http://127.0.0.1:18093/api/config | head -c 400; echo
echo "--- GPU ---"
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader
echo "--- 최근 로그 ---"
SUDO docker logs --tail 25 taxitalk-web
'@

if (-not $SkipUpload) { Send-Project }

Write-Host '== .env 확인 ==' -ForegroundColor Cyan
Invoke-Remote $EnsureEnv

if ($SetEnv.Count) {
    Write-Host '== .env 값 덮어쓰기 ==' -ForegroundColor Cyan
    $pairs = foreach ($entry in $SetEnv) {
        if ($entry -notmatch '^\s*(\w+)\s*=\s*(.*?)\s*$') {
            throw "-SetEnv 는 KEY=VALUE 형태여야 합니다: $entry"
        }
        # sed 구분자로 '|' 를 쓰므로 값에 섞이면 치환식이 깨진다.
        if ($Matches[2] -like '*|*') { throw "-SetEnv 값에 '|' 는 쓸 수 없습니다: $entry" }
        "apply '{0}' '{1}'" -f $Matches[1], $Matches[2]
    }
    Invoke-Remote ($ApplyEnv -replace '__PAIRS__', ($pairs -join "`n"))
}

if (-not $SkipBuild) {
    Write-Host '== 빌드/기동 ==' -ForegroundColor Cyan
    Invoke-Remote $BuildUp
}

if (-not $SkipNginx) {
    Write-Host '== nginx 연결 ==' -ForegroundColor Cyan
    Invoke-Remote $Nginx
}

if (-not $SkipHealth) {
    Write-Host '== 헬스체크 ==' -ForegroundColor Cyan
    Invoke-Remote $Health
}

Write-Host ''
Write-Host '배포 완료: https://ai.tovair.com:48443/demo/taxitalk/' -ForegroundColor Green
