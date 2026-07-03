param(
  [string]$NasHost = $(if ($env:FUND_REPORT_NAS_HOST) { $env:FUND_REPORT_NAS_HOST } else { $env:STOCK_REPORT_NAS_HOST }),
  [string]$NasUser = $(if ($env:FUND_REPORT_NAS_USER) { $env:FUND_REPORT_NAS_USER } elseif ($env:STOCK_REPORT_NAS_USER) { $env:STOCK_REPORT_NAS_USER } else { "root" }),
  [string]$RemoteRoot = $(if ($env:FUND_REPORT_REMOTE_ROOT) { $env:FUND_REPORT_REMOTE_ROOT } elseif ($env:STOCK_REPORT_REMOTE_ROOT) { $env:STOCK_REPORT_REMOTE_ROOT } else { "/volume1/docker/fund-report" }),
  [switch]$Rebuild
)

$ErrorActionPreference = "Stop"

if (-not $NasHost) {
  throw "Missing NAS host. Pass -NasHost <host> or set FUND_REPORT_NAS_HOST."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$archive = Join-Path $env:TEMP "fund-report-deploy.tar.gz"
$remoteRoot = $RemoteRoot
$remoteApp = "$remoteRoot/app"
$remoteData = "$remoteRoot/data"
$remoteEnv = "$remoteRoot/fund-report.env"
$sshTarget = "$NasUser@$NasHost"

Push-Location $root
try {
  Get-ChildItem -Path $root -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  if (Test-Path $archive) {
    Remove-Item -LiteralPath $archive -Force
  }

  tar -czf $archive --exclude __pycache__ --exclude .pytest_cache --exclude .venv --exclude data --exclude logs --exclude uploads --exclude .git --exclude .env --exclude ".env.*" --exclude "*.sqlite" --exclude "*.sqlite3" --exclude "*.db" --exclude "*.log" --exclude "*.pem" --exclude "*.key" --exclude AGENTS.md --exclude .agents .
  scp -O $archive "${sshTarget}:${remoteApp}/fund-report-deploy.tar.gz"

  $rebuildFlag = if ($Rebuild) { "1" } else { "0" }
  $remote = @"
set -e
mkdir -p '$remoteApp' '$remoteData'
cd '$remoteApp'
tar -xzf fund-report-deploy.tar.gz
rm -f fund-report-deploy.tar.gz
if [ '$rebuildFlag' = '1' ]; then
  /usr/local/bin/docker build --build-arg BASE_IMAGE=docker.1ms.run/library/python:3.11-slim -t fund-report:latest .
fi
env_file_args=
if [ -f '$remoteEnv' ]; then
  env_file_args=--env-file=$remoteEnv
fi
/usr/local/bin/docker stop fund-report >/dev/null 2>&1 || true
/usr/local/bin/docker rm fund-report >/dev/null 2>&1 || true
/usr/local/bin/docker run -d \
  --name fund-report \
  --restart unless-stopped \
  -p 8088:8088 \
  -v '${remoteData}:/data' \
  -v '${remoteApp}/app:/app/app' \
  -e TZ=Asia/Shanghai \
  `$env_file_args \
  fund-report:latest
"@

  ssh $sshTarget $remote
}
finally {
  Pop-Location
}
