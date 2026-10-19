# CloudCampus

**Serverless campus event-management platform for students and organizers.**

Students discover, search and register for campus events. Organizers create
events, track capacity and view attendees. Everything runs on AWS serverless
services with no servers to manage.

> **Live demo:** <https://dxkntx3cx6c5a.cloudfront.net>
> (Sign up with any email address to browse as a student; see
> [Organizer promotion](#organizer-promotion) to unlock the organizer dashboard.)

---

## Table of contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Quick start](#quick-start)
- [Deploy to AWS](#deploy-to-aws)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Organizer promotion](#organizer-promotion)
- [Sample data](#sample-data)
- [Tests](#tests)
- [Cost and scope](#cost-and-scope)
- [Security notes](#security-notes)
- [Documentation](#documentation)
- [License](#license)

---

## Features

### Students
- Browse published events, search by title/description, filter by category.
- Event detail view with seats remaining and the registration deadline.
- Sign up / sign in / sign out through the Cognito hosted UI with PKCE.
- Register for an event, cancel a registration, and review "My Registrations".
- Full loading / empty / error / full states.

### Organizers (server-verified `organizers` Cognito group)
- Organizer dashboard listing **only the events you own**, across draft,
  published and cancelled states.
- Create events with title, description, category, date/time, location,
  capacity and registration deadline.
- Cancel events and view the attendee list with live counts.
- Load reproducible sample data.

### Engineering guarantees
- Registration is **atomic**: capacity, event status and the deadline are
  validated and the seat counter incremented inside a single DynamoDB
  `TransactWriteItems` call, so concurrent requests cannot over-book an event.
- Duplicate registration is rejected by a conditional write on the
  registration row; re-registering after a cancellation is allowed.
- Cancelling a registration flips the row and decrements the counter in one
  transaction, so the two can never drift apart.
- Ownership is enforced in the backend with DynamoDB `ConditionExpression` on
  `organizerId`. Frontend role checks are display-only.
- Every DynamoDB read follows `LastEvaluatedKey`, so nothing is silently
  truncated at the 1 MB page boundary.

---

## Architecture

```text
Browser (React SPA)
   |                     |
   | fetch + Bearer JWT  | HTTPS (OAC)
   v                     v
API Gateway HTTP API   CloudFront -> private S3 bucket
   |  (JWT authorizer: Cognito)
   v
Lambda (python3.12, app.handler)
   |
   v
DynamoDB (single table, PAY_PER_REQUEST, SSE)
```

| Item | Value |
|---|---|
| Stack | `cloudcampus-prod` |
| Region | `ap-south-1` (Mumbai) — configurable, see [Configuration](#configuration) |
| Frontend | private S3 + CloudFront (OAC, SPA fallback, HTTPS redirect) |
| API | API Gateway HTTP API, Cognito JWT authorizer |
| Auth | Cognito user pool, hosted UI + PKCE, `organizers` group |
| Data | DynamoDB single table, three GSIs |
| Deploy | `scripts/deploy.ps1` (AWS CLI only, no SAM CLI) |

Everything is regional (`ap-south-1` by default) except CloudFront, which is a
global service by design. Full detail:
[`docs/architecture.md`](docs/architecture.md).

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | React 19 + TypeScript + Vite (no AWS SDK, no Amplify) |
| Backend | Python 3.12 single Lambda, boto3 only |
| Auth | Amazon Cognito (hosted UI, authorization code + PKCE) |
| API | Amazon API Gateway HTTP API + JWT authorizer |
| Data | Amazon DynamoDB (single-table design, 3 GSIs) |
| Hosting | Amazon S3 (private) + Amazon CloudFront (OAC) |
| Infra | AWS CloudFormation (single template, no serverless framework) |
| Tooling | AWS CLI v2, npm, Python 3.11+ for tests |

---

## Repository layout

```text
backend/app.py           entire API: routes, auth, validation, data access
backend/seed_data.py     deterministic sample events (shared by seed + tests)
infra/template.yaml      CloudFormation stack (pure, validate-template clean)
frontend/src/            React 19 + TypeScript + Vite SPA
  auth.ts                Cognito hosted UI + PKCE + refresh + logout
  app-shell.tsx          API client, token lifecycle, header
  views-events.tsx       discover list + event detail
  views-manage.tsx       my registrations + organizer dashboard
scripts/deploy.ps1       idempotent 8-step deployment pipeline
tests/test_backend.py    59 unit tests
tests/e2e_live.py        end-to-end checks against a deployed stack
tests/fakes.py           in-memory DynamoDB that enforces real DynamoDB rules
docs/                    architecture and deployment documentation
```

---

## Quick start

### Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Node.js | 20+ | building/running the frontend |
| Python | 3.11+ | running the backend unit tests |
| AWS CLI | 2.35+ | deploying to AWS |

### Clone

```powershell
git clone <your-repo-url> cloudcampus
cd cloudcampus
```

### Run the backend unit tests (no AWS calls)

```powershell
py -3.11 tests/test_backend.py
```

### Run the frontend locally

The committed `frontend/.env.production` points at the live demo backend, so a
fresh clone can run against it with no AWS setup:

```powershell
Copy-Item frontend/.env.production frontend/.env.local
npm --prefix frontend install
npm --prefix frontend run dev      # http://localhost:5173
```

The Cognito app client already registers `http://localhost:5173/` as a callback,
so sign-in works from the dev server. To point local dev at your **own** stack
instead, see [Configuration](#configuration).

---

## Deploy to AWS

Authenticate first, then run the pipeline:

```powershell
# auto-rotating session, no static keys
aws login
aws sts get-caller-identity

# one command, idempotent, re-runnable
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
```

The script packages the backend, deploys the CloudFormation stack, ensures the
Cognito hosted-UI domain, writes `frontend/.env.production`, builds and uploads
the SPA, tightens CORS to the live CloudFront origin, and smoke-tests the API.
It writes `deployment-outputs.json` (git-ignored) with the resulting URLs and
ids.

Full walkthrough, manual equivalent, verification and troubleshooting:
[`docs/deployment.md`](docs/deployment.md).

### Teardown

```powershell
aws cloudformation delete-stack --stack-name cloudcampus-prod --region ap-south-1
```

---

## Configuration

`scripts/deploy.ps1` reads optional environment overrides:

| Variable | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | `opencode_Agent` | Named AWS CLI profile. **Set this to your own profile**, or to an empty string to use the default credential chain. |
| `AWS_REGION` | `ap-south-1` | Region for every regional resource. |
| `PROJECT` | `cloudcampus` | Resource-name prefix. |
| `STACK` | `cloudcampus-prod` | CloudFormation stack name. |
| `LOCALHOST_ORIGIN` | `http://localhost:5173` | Extra Cognito callback for local dev. |

```powershell
$env:AWS_PROFILE = '<your-profile>'
$env:AWS_REGION  = 'ap-south-1'
powershell -ExecutionPolicy Bypass -File scripts/deploy.ps1
```

> **Region:** all regional resources are created in the region your project is
> assigned. Confirm yours in **AWS Settings → View all projects → Overview →
> Additional Info → Region** before deploying, and set `AWS_REGION` to match.
> Cross-region resources are intentionally not used.

Frontend build-time variables live in `frontend/.env.production` (generated by
the deploy script). They are all **public identifiers** — there are no secrets.
See [`frontend/.env.example`](frontend/.env.example) for the full list.

---

## API reference

| Method | Route | Auth |
|---|---|---|
| GET | `/health` | public |
| GET | `/events` | public (`?status=`, `?category=`, `?search=`) |
| GET | `/events/{id}` | public |
| POST | `/events` | organizer |
| PATCH | `/events/{id}` | organizer, owner |
| POST | `/events/{id}/cancel` | organizer, owner |
| POST | `/events/{id}/register` | any signed-in user |
| POST | `/events/{id}/unregister` | any signed-in user |
| GET | `/me/registrations` | any signed-in user |
| GET | `/me/events` | any signed-in user (organizer view) |
| GET | `/events/{id}/attendees` | organizer, owner |
| POST | `/seed` | organizer |

Every response is JSON: `{"success": true, "data": ...}` or
`{"success": false, "error": "...", "message": "..."}`.

---

## Organizer promotion

The browser never self-assigns roles. To make an account an organizer:

```powershell
aws cognito-idp admin-add-user-to-group `
  --region ap-south-1 `
  --user-pool-id <UserPoolId> `
  --username <their-email> `
  --group-name organizers
```

The user must then sign out and sign back in, because the `cognito:groups`
claim is only minted at login. `<UserPoolId>` is in `deployment-outputs.json`
after a deploy.

---

## Sample data

`POST /seed` (organizer-only) loads seven clearly fictional events covering
different categories, capacities and statuses, plus synthetic attendees. It is
idempotent: re-running skips events that already exist and never deletes
anything.

```text
{}                    -> create missing seed events only
{"reset": true}       -> refresh seed rows in place; refuses if any real user
                         registration exists on those events
```

Sample data lives in `backend/seed_data.py` and is shared with the test suite,
so local fixtures and production seed data cannot drift apart.

---

## Tests

```powershell
py -3.11 tests/test_backend.py   # 59 tests, no AWS calls
npm --prefix frontend run build  # production build + typecheck
npm --prefix frontend run lint   # oxlint
```

`tests/e2e_live.py` exercises a **deployed** stack end to end (real Cognito
users, real API calls, concurrent-registration race) and cleans up after itself:

```powershell
py -3.11 tests/e2e_live.py --profile <your-profile> --region ap-south-1
```

`tests/fakes.py` is a small in-memory DynamoDB that enforces the two rules the
original registration code violated (no two operations on one item in a
transaction, no unused expression attribute entries), which is why the parallel
capacity test is meaningful rather than decorative.

---

## Cost and scope

Built for a free-tier / low-cost demo: on-demand DynamoDB, 7-day log retention,
no EC2, RDS, NAT, ALB, EKS or OpenSearch, and no paid add-ons. The Cognito hosted
UI, CloudFront distribution and Lambda are the only always-on resources.

---

## Security notes

- No AWS credentials, keys or tokens are stored in this repository.
- `frontend/.env.production` holds only public identifiers (API URL, user pool
  id, app client id, hosted UI domain). These are public by design in a PKCE
  SPA and are not secrets.
- The Lambda execution role is scoped to the one DynamoDB table and its
  indexes; no wildcard resource ARNs.
- The S3 site bucket has public access blocked and is reachable only through
  CloudFront via an Origin Access Control.

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — data model, concurrency,
  auth model, API surface.
- [`docs/deployment.md`](docs/deployment.md) — deployment walkthrough,
  verification and troubleshooting.
- [`frontend/README.md`](frontend/README.md) — frontend scripts and source map.

---

## License

Released under the [MIT License](LICENSE).
