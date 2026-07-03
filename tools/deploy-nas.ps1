param(
  [string]$NasHost = $env:STOCK_REPORT_NAS_HOST,
  [string]$NasUser = $(if ($env:STOCK_REPORT_NAS_USER) { $env:STOCK_REPORT_NAS_USER } else { "root" }),
  [string]$RemoteRoot = $(if ($env:STOCK_REPORT_REMOTE_ROOT) { $env:STOCK_REPORT_REMOTE_ROOT } else { "/volume1/docker/stock-report" }),
  [switch]$Rebuild
)

$ErrorActionPreference = "Stop"

if (-not $NasHost) {
  throw "Missing NAS host. Pass -NasHost <host> or set STOCK_REPORT_NAS_HOST."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$archive = Join-Path $env:TEMP "stock-report-deploy.tar.gz"
$remoteRoot = $RemoteRoot
$remoteApp = "$remoteRoot/app"
$remoteData = "$remoteRoot/data"
$remoteEnv = "$remoteRoot/stock-report.env"
$sshTarget = "$NasUser@$NasHost"

Push-Location $root
try {
  Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
  }

  tar -czf $archive --exclude __pycache__ --exclude .pytest_cache --exclude .venv --exclude data --exclude logs --exclude uploads --exclude .git --exclude .env --exclude ".env.*" --exclude "*.sqlite" --exclude "*.sqlite3" --exclude "*.db" --exclude "*.log" --exclude "*.pem" --exclude "*.key" --exclude AGENTS.md --exclude .agents .
  scp -O $archive "${sshTarget}:${remoteApp}/stock-report-deploy.tar.gz"

  $rebuildFlag = if ($Rebuild) { "1" } else { "0" }
  $remote = @"
set -e
mkdir -p '$remoteApp' '$remoteData'
cd '$remoteApp'
tar -xzf stock-report-deploy.tar.gz
rm -f stock-report-deploy.tar.gz
if [ '$rebuildFlag' = '1' ]; then
  /usr/local/bin/docker build --build-arg BASE_IMAGE=docker.1ms.run/library/python:3.11-slim -t stock-report:latest .
fi
env_file_args=
if [ -f '$remoteEnv' ]; then
  env_file_args=--env-file=$remoteEnv
fi
/usr/local/bin/docker stop stock-report >/dev/null 2>&1 || true
/usr/local/bin/docker rm stock-report >/dev/null 2>&1 || true
/usr/local/bin/docker run -d \
  --name stock-report \
  --restart unless-stopped \
  -p 8088:8088 \
  -v '${remoteData}:/data' \
  -v '${remoteApp}/app:/app/app' \
  -e TZ=Asia/Shanghai \
  `$env_file_args \
  stock-report:latest
"@

  ssh $sshTarget $remote
}
finally {
  Pop-Location
}
