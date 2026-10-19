# CloudCampus deploy pipeline (Windows PowerShell, AWS CLI only, no SAM CLI).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
#
# Optional env overrides: $env:AWS_PROFILE (default opencode_Agent),
# $env:AWS_REGION (default ap-south-1), $env:PROJECT (default cloudcampus),
# $env:STACK (default cloudcampus-prod), $env:LOCALHOST_ORIGIN (default
# http://localhost:5173).
#
# The pipeline is idempotent: it can be re-run against an existing stack.
$ErrorActionPreference = 'Stop'
$Profile = if ($env:AWS_PROFILE) { $env:AWS_PROFILE } else { 'opencode_Agent' }
$Region = if ($env:AWS_REGION) { $env:AWS_REGION } else { 'ap-south-1' }
$Project = if ($env:PROJECT) { $env:PROJECT } else { 'cloudcampus' }
$Stack = if ($env:STACK) { $env:STACK } else { 'cloudcampus-prod' }
$LocalhostOrigin = if ($env:LOCALHOST_ORIGIN) { $env:LOCALHOST_ORIGIN } else { 'http://localhost:5173' }
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# NOTE: do not name this function "Aws". PowerShell command resolution is
# case-insensitive, so `& aws ...` inside a function called `Aws` recurses into
# itself and dies with a call-depth overflow.
function Invoke-Aws([string[]]$CliArgs) {
  # PowerShell 5.1 turns a non-zero exit code from a native command into a
  # terminating error when $ErrorActionPreference='Stop'. Temporarily relax
  # it around the call and rely on $LASTEXITCODE instead, so that stderr
  # traffic (which `aws cloudformation deploy` emits for progress) does not
  # abort an otherwise-successful invocation.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $errFile = [System.IO.Path]::GetTempFileName()
    $global:LASTEXITCODE = 0
    $out = & aws --profile $Profile --region $Region @CliArgs 2> $errFile
    $code = $LASTEXITCODE
    $err = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { '' }
    Remove-Item $errFile -Force -ErrorAction SilentlyContinue
    if ($code -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $code):`n$err`n$out" }
    return $out
  } finally { $ErrorActionPreference = $prev }
}
function Invoke-AwsJson([string[]]$CliArgs) {
  return (Invoke-Aws $CliArgs) | ConvertFrom-Json
}

Write-Host "== 1/8 verify identity =="
Invoke-Aws @('sts', 'get-caller-identity', '--output', 'json') | Out-Null
$AccountId = (Invoke-AwsJson @('sts', 'get-caller-identity', '--output', 'json')).Account

Write-Host "== 2/8 package backend =="
# Every module under backend/ ships (app.py imports seed_data), so stage the
# directory's *.py and zip them flat, otherwise only the last file survives.
$zip = Join-Path $Root 'backend.zip'
$stage = Join-Path ([System.IO.Path]::GetTempPath()) 'cloudcampus-backend'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stage | Out-Null
Get-ChildItem (Join-Path $Root 'backend') -Filter *.py | Copy-Item -Destination $stage
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
$zipped = (Get-ChildItem $stage -Filter *.py).Name -join ', '
Write-Host "packaged: $zipped"
$CodeBucket = "$Project-code-$AccountId-$Region".ToLower()
try { Invoke-Aws @('s3api', 'head-bucket', '--bucket', $CodeBucket) | Out-Null }
catch { Invoke-Aws @('s3api', 'create-bucket', '--bucket', $CodeBucket, '--create-bucket-configuration', "LocationConstraint=$Region") | Out-Null }
$CodeKey = "backend/$((Get-Date).ToString('yyyyMMdd-HHmmss'))/backend.zip"
Invoke-Aws @('s3', 'cp', $zip, "s3://$CodeBucket/$CodeKey") | Out-Null
Write-Host "code: s3://$CodeBucket/$CodeKey"

Write-Host "== 3/8 deploy stack (pass 1: permissive CORS) =="
$tpl = Join-Path $Root 'infra/template.yaml'
$params1 = @('ProjectName=' + $Project, 'CodeS3Bucket=' + $CodeBucket, 'CodeS3Key=' + $CodeKey, 'FrontendOrigin=*', 'LocalhostOrigin=' + $LocalhostOrigin)
Invoke-Aws (@('cloudformation', 'deploy', '--stack-name', $Stack, '--template-file', $tpl, '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameter-overrides') + $params1) | Out-Null
Write-Host "stack deployed: $Stack"

Write-Host "== 4/8 read stack outputs =="
$outs = Invoke-AwsJson @('cloudformation', 'describe-stacks', '--stack-name', $Stack, '--query', 'Stacks[0].Outputs', '--output', 'json')
$get = { param($k) ($outs | Where-Object { $_.OutputKey -eq $k }).OutputValue }
$ApiUrl = &$get 'ApiUrl'; $PoolId = &$get 'UserPoolId'; $ClientId = &$get 'UserPoolClientId'
$Bucket = &$get 'SiteBucketName'; $DistId = &$get 'DistributionId'; $Front = &$get 'FrontendUrl'

Write-Host "== 5/8 ensure Cognito hosted-UI domain =="
# Managed here rather than in the template: a prefix domain is global per user
# pool, so CloudFormation cannot adopt one that already exists. Idempotent.
$DomainPrefix = "$Project-$AccountId".ToLower()
$DomainOut = (& aws --profile $Profile --region $Region cognito-idp describe-user-pool-domain --user-pool-id $PoolId --domain $DomainPrefix --output json 2>&1) | ConvertFrom-Json
if (-not $DomainOut.DomainDescription) {
  Invoke-Aws @('cognito-idp', 'create-user-pool-domain', '--user-pool-id', $PoolId, '--domain', $DomainPrefix) | Out-Null
  Write-Host "created hosted UI domain: $DomainPrefix"
} else {
  Write-Host "hosted UI domain already present: $DomainPrefix"
}
$HostedUi = "$DomainPrefix.auth.$Region.amazoncognito.com"

Write-Host "== 6/8 write frontend env =="
@"
VITE_API_URL=$ApiUrl
VITE_COGNITO_REGION=$Region
VITE_COGNITO_USER_POOL_ID=$PoolId
VITE_COGNITO_CLIENT_ID=$ClientId
VITE_COGNITO_DOMAIN=$HostedUi
"@ | Out-File frontend/.env.production -Encoding utf8
Write-Host "api=$ApiUrl front=$Front cognito=$HostedUi"

Write-Host "== 7/8 build + upload frontend =="
Push-Location (Join-Path $Root 'frontend')
try {
  if (-not (Test-Path node_modules)) { npm ci | Out-Null }
  npm run build
  if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
} finally { Pop-Location }
Invoke-Aws @('s3', 'sync', 'frontend/dist/', "s3://$Bucket/", '--delete') | Out-Null
Invoke-Aws @('cloudfront', 'create-invalidation', '--distribution-id', $DistId, '--paths', '/*') | Out-Null

Write-Host "== 8/8 tighten CORS to the live CloudFront origin =="
$params2 = @('ProjectName=' + $Project, 'CodeS3Bucket=' + $CodeBucket, 'CodeS3Key=' + $CodeKey, 'FrontendOrigin=' + $Front, 'LocalhostOrigin=' + $LocalhostOrigin)
Invoke-Aws (@('cloudformation', 'deploy', '--stack-name', $Stack, '--template-file', $tpl, '--capabilities', 'CAPABILITY_NAMED_IAM', '--parameter-overrides') + $params2) | Out-Null

Write-Host "== smoke test =="
$health = Invoke-RestMethod "$ApiUrl/health"
$events = Invoke-RestMethod "$ApiUrl/events"
Write-Host ("health = " + ($health | ConvertTo-Json -Compress))
Write-Host ("events = " + (@($events.data).Count) + " item(s)")

$summary = [ordered]@{
  stack = $Stack; region = $Region; account = $AccountId
  apiUrl = $ApiUrl; frontendUrl = $Front
  userPoolId = $PoolId; clientId = $ClientId; cognitoDomain = $HostedUi
  bucket = $Bucket; distributionId = $DistId; codeLocation = "s3://$CodeBucket/$CodeKey"
}
$summary | ConvertTo-Json | Out-File (Join-Path $Root 'deployment-outputs.json') -Encoding utf8
Write-Host "DONE. See deployment-outputs.json"