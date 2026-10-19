# Deploying CloudCampus

Everything runs from one script. No SAM CLI, no CDK, no manual console steps.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Git | any | to clone the repository |
| AWS CLI | 2.35+ | `aws --version` |
| Node.js | 20+ | `node --version` |
| Python | 3.11+ | only needed for the unit tests |

```powershell
git clone <your-repo-url> cloudcampus
cd cloudcampus
```

Authenticate to AWS, then confirm the identity and region the deploy will use:

```powershell
aws login                      # auto-rotating session, no static keys
aws sts get-caller-identity
aws configure get region
```

> **Region:** all regional resources are created in the region your project is
> assigned. Confirm yours in **AWS Settings → View all projects → Overview →
> Additional Info → Region**, and either set `$env:AWS_REGION` or run
> `aws configure` so it matches. The stack defaults to `ap-south-1`.
>
> CloudFront is a global service and is created regardless of region; Lambda,
> API Gateway, DynamoDB and Cognito always land in the selected region.

## One command

```powershell
$env:AWS_PROFILE = '<your-profile>'   # omit to use the default credential chain
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
```

The script is idempotent: it can be re-run against an existing stack and will
update in place rather than replacing resources.

### Overrides

| Variable | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | `opencode_Agent` | Named AWS CLI profile. Set it to your own profile, or leave unset to use the default credential chain. |
| `AWS_REGION` | `ap-south-1` | Region for every regional resource. |
| `PROJECT` | `cloudcampus` | Resource name prefix. |
| `STACK` | `cloudcampus-prod` | CloudFormation stack name. |
| `LOCALHOST_ORIGIN` | `http://localhost:5173` | Vite dev-server origin kept as a Cognito callback. |

```powershell
$env:AWS_PROFILE = '<your-profile>'
$env:AWS_REGION  = 'ap-south-1'
$env:PROJECT     = 'cloudcampus'
$env:STACK       = 'cloudcampus-prod'
$env:LOCALHOST_ORIGIN = 'http://localhost:5173'
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
```

## What the script does

1. **Verify identity** - fails fast if credentials are not usable.
2. **Package the backend** - stages every `backend/*.py` into a temp folder and
   zips them flat, then uploads to `s3://<project>-code-<account>-<region>/backend/<timestamp>/backend.zip`.
   `app.py` imports `seed_data`, so both files must ship together.
3. **Deploy the stack (pass 1)** with `FrontendOrigin=*`, because the
   CloudFront URL is not known until the distribution exists.
4. **Read stack outputs.**
5. **Ensure the Cognito hosted UI domain** - `describe-user-pool-domain`, and
   only create it if it is absent. This is managed here rather than in the
   template because a Cognito prefix domain is unique per user pool, so
   CloudFormation cannot adopt one that already exists.
6. **Write `frontend/.env.production`** from the stack outputs, including
   `VITE_COGNITO_DOMAIN`. All values are public identifiers, not secrets.
7. **Build and publish the frontend** - `npm run build`, `s3 sync --delete`,
   then a CloudFront invalidation on `/*`.
8. **Deploy the stack (pass 2)** with the real CloudFront origin, so API
   Gateway CORS and `FRONTEND_ORIGIN` on Lambda are locked down.

Finally it smoke tests `/health` and `/events` and writes the local, git-ignored
`deployment-outputs.json` (stack, region, account, API URL, frontend URL, user
pool id, client id, Cognito domain, bucket, distribution id, code location).

## Manual equivalent

Useful when you want to see each step, or when the script is unavailable.
Replace the `<...>` placeholders with your own values (they are all printed by
`deployment-outputs.json` after a scripted deploy).

```powershell
$Region  = 'ap-south-1'
$Profile = '<your-profile>'
$Account = '<account-id>'

# 1. package + upload backend
Compress-Archive -Path backend\app.py, backend\seed_data.py -Destination backend.zip -Force
aws s3 cp backend.zip `
  s3://cloudcampus-code-$Account-$Region/backend/<timestamp>/backend.zip `
  --profile $Profile --region $Region

# 2. update the stack
aws cloudformation deploy --profile $Profile --region $Region `
  --stack-name cloudcampus-prod --template-file infra\template.yaml `
  --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides `
    ProjectName=cloudcampus `
    CodeS3Bucket=cloudcampus-code-$Account-$Region `
    CodeS3Key=backend/<timestamp>/backend.zip `
    FrontendOrigin=https://<distribution>.cloudfront.net `
    LocalhostOrigin=http://localhost:5173

# 3. write the frontend env from stack outputs
aws cloudformation describe-stacks --profile $Profile --region $Region `
  --stack-name cloudcampus-prod --query 'Stacks[0].Outputs' --output json

# 4. build + publish
npm --prefix frontend run build
aws s3 sync frontend\dist\ s3://cloudcampus-site-$Account-$Region/ `
  --delete --profile $Profile
aws cloudfront create-invalidation --profile $Profile `
  --distribution-id <distribution-id> --paths '/*'

# 5. verify
curl.exe -s https://<api-id>.execute-api.$Region.amazonaws.com/health
curl.exe -s https://<api-id>.execute-api.$Region.amazonaws.com/events
```

## Verifying a deployment

```powershell
# stack
aws cloudformation describe-stacks --profile <your-profile> --region ap-south-1 `
  --stack-name cloudcampus-prod --query 'Stacks[0].[StackStatus,LastUpdatedTime]'

# Lambda code actually deployed
aws lambda get-function --profile <your-profile> --region ap-south-1 `
  --function-name cloudcampus-api --query 'Configuration.[Runtime,Handler,Environment]'

# DynamoDB indexes
aws dynamodb describe-table --profile <your-profile> --region ap-south-1 `
  --table-name cloudcampus-events --query 'Table.[TableStatus,GlobalSecondaryIndexes[].IndexName]'

# live endpoints
curl.exe -s https://<api-id>.execute-api.ap-south-1.amazonaws.com/health
curl.exe -s https://<distribution>.cloudfront.net/

# runtime errors
aws logs filter-log-events --profile <your-profile> --region ap-south-1 `
  --log-group-name /aws/lambda/cloudcampus-api --filter-pattern 'ERROR' --max-items 20
```

A full end-to-end check against the deployed API lives in
[`tests/e2e_live.py`](../tests/e2e_live.py):

```powershell
py -3.11 tests/e2e_live.py --profile <your-profile> --region ap-south-1
```

## After the first deploy

Sign in with a browser account, then promote it to organizer:

```powershell
aws cognito-idp admin-add-user-to-group --profile <your-profile> --region ap-south-1 `
  --user-pool-id <UserPoolId> --username <email> --group-name organizers
```

The user must sign out and sign back in before the `cognito:groups` claim
reappears.

Then load sample data from the organizer dashboard, or:

```powershell
# requires a signed-in organiser token
curl.exe -s -X POST https://<api-id>.execute-api.ap-south-1.amazonaws.com/seed `
  -H "Authorization: Bearer <access-token>" -H "Content-Type: application/json" -d '{}'
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `The parameter templateBody ... invalid type` | `validate-template` needs `--template-body file://<forward-slash-path>`; use `--template-file` for `cloudformation deploy`. |
| `An error occurred (ExpiredToken)` | Run `aws login` again. |
| `AccessDenied` on an action that used to work | The project may have hit a spend limit. Check **AWS Settings → Billing**. Only a project owner can raise it. |
| Frontend signs in then bounces back | `VITE_COGNITO_DOMAIN` missing from `frontend/.env.production`, or the CloudFront URL is not in `CallbackURLs`. Both are handled by the deploy script. |
| `401` from the API on a valid session | Access token expired and the client did not refresh; hard-refresh the tab. |
| `409 Already registered` | Expected: the user is already registered for that event. |
| DynamoDB `ValidationException` on registration | A transaction now touches the same item twice, or references an unused expression attribute value. `tests/test_backend.py::TransactionShapeTests` guards both. |
| Deploy times out at 30 s | `cloudformation deploy` blocks until the stack settles. Run it in the background and poll `describe-stacks` instead. |

## Teardown

```powershell
aws cloudformation delete-stack --profile <your-profile> --region ap-south-1 `
  --stack-name cloudcampus-prod
aws s3 rb s3://cloudcampus-code-<account-id>-ap-south-1 --force
```

The Cognito user pool and its hosted UI domain survive a stack delete by
default; remove them separately if you want a clean slate.
