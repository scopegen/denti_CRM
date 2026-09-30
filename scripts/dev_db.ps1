# Denti's own local PostgreSQL, separate from any other Postgres on this machine.
# Data lives in backend/.pgdata (gitignored) and the server listens on port 5434.
#
#   .\scripts\dev_db.ps1 init    # first time only: create the cluster, roles and databases
#   .\scripts\dev_db.ps1 start
#   .\scripts\dev_db.ps1 stop
#   .\scripts\dev_db.ps1 status
#
# Roles (see docs/database-design.md, section 2):
#   denti_owner  owns the tables; used only by Alembic migrations
#   denti_app    what the API connects as; cannot bypass row-level security

param([Parameter(Mandatory = $true)][ValidateSet('init', 'start', 'stop', 'status')][string]$Command)

$ErrorActionPreference = 'Stop'
$pgBin = if ($env:PG_BIN) { $env:PG_BIN } else { 'C:\Program Files\PostgreSQL\18\bin' }
$backendDir = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $backendDir '.pgdata'
$logFile = Join-Path $backendDir '.pgdata.log'
$port = 5434

# Local development passwords only. They are never used anywhere else.
$superPassword = 'denti_super_dev'
$ownerPassword = 'denti_owner_dev'
$appPassword = 'denti_app_dev'

function Invoke-Pg([string]$exe, [string[]]$pgArgs) {
  & (Join-Path $pgBin $exe) @pgArgs
  if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

function Invoke-Sql([string]$database, [string]$sql) {
  $env:PGPASSWORD = $superPassword
  try {
    Invoke-Pg 'psql.exe' @('-h', 'localhost', '-p', "$port", '-U', 'postgres', '-d', $database, '-v', 'ON_ERROR_STOP=1', '-q', '-c', $sql)
  } finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  }
}

function Initialize-Database([string]$name) {
  Invoke-Sql 'postgres' "CREATE DATABASE $name OWNER denti_owner ENCODING 'UTF8' TEMPLATE template0;"
  Invoke-Sql $name @"
REVOKE ALL ON DATABASE $name FROM PUBLIC;
GRANT CONNECT ON DATABASE $name TO denti_owner, denti_app;
ALTER SCHEMA public OWNER TO denti_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO denti_app;
ALTER DEFAULT PRIVILEGES FOR ROLE denti_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO denti_app;
ALTER DEFAULT PRIVILEGES FOR ROLE denti_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO denti_app;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
"@
}

switch ($Command) {
  'init' {
    if (Test-Path $dataDir) { throw "$dataDir already exists - the cluster is already initialised." }
    $pwFile = New-TemporaryFile
    try {
      Set-Content -Path $pwFile -Value $superPassword -NoNewline -Encoding ascii
      Invoke-Pg 'initdb.exe' @('-D', $dataDir, '-U', 'postgres', "--pwfile=$pwFile", '-E', 'UTF8', '--locale=C', '--auth=scram-sha-256')
    } finally {
      Remove-Item $pwFile -Force
    }
    Invoke-Pg 'pg_ctl.exe' @('-D', $dataDir, '-o', "-p $port", '-l', $logFile, '-w', 'start')
    Invoke-Sql 'postgres' @"
CREATE ROLE denti_owner LOGIN PASSWORD '$ownerPassword';
CREATE ROLE denti_app LOGIN PASSWORD '$appPassword' NOSUPERUSER NOBYPASSRLS;
"@
    Initialize-Database 'denti_dev'
    Initialize-Database 'denti_test'
    Write-Host "Denti database ready on port $port (databases: denti_dev, denti_test)."
  }
  'start' { Invoke-Pg 'pg_ctl.exe' @('-D', $dataDir, '-o', "-p $port", '-l', $logFile, '-w', 'start') }
  'stop' { Invoke-Pg 'pg_ctl.exe' @('-D', $dataDir, '-w', 'stop') }
  'status' { & (Join-Path $pgBin 'pg_ctl.exe') -D $dataDir status }
}
