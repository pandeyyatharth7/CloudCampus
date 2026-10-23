---
title: "CloudCampus"
tagline: "Serverless campus events with race-free registration"
description: "A serverless campus event platform where students discover and register for events while organizers manage capacity, built on React, a single Python Lambda, DynamoDB, Cognito, and CloudFront."
category: "Cloud / Full Stack"
status: "live"
featured: false
technologies:
  - React 19
  - TypeScript
  - Vite
  - Python 3.12
  - AWS Lambda
  - Amazon API Gateway
  - Amazon DynamoDB
  - Amazon Cognito
  - Amazon CloudFront
  - AWS CloudFormation
demoUrl: "https://dxkntx3cx6c5a.cloudfront.net"
---

# Project Overview

CloudCampus is a serverless campus event-management platform for students and organizers. Students browse and search published events, check remaining seats and registration deadlines, then register or cancel from a single account. Organizers create events, cancel them, and view attendee lists scoped to the events they own.

The project targets the common campus workflow of coordinating events through spreadsheets, forms, and manual seat counting. It centralizes discovery, registration, and capacity tracking in one deployed application with authentication and server-side authorization.

## Problem Statement

Event registration is a concurrency problem as much as a UI problem. When several students register at the same moment, a naive read-then-write flow can over-book an event, lose a seat, or let someone register twice. CloudCampus treats capacity, event status, and the registration deadline as invariants enforced by the database rather than by client-side checks.

## Architecture and Approach

- **Frontend:** a React 19 + TypeScript single-page app built with Vite. It is hosted in a private S3 bucket and served through CloudFront using Origin Access Control, with SPA fallback routing and an HTTPS redirect.
- **API and auth:** API Gateway HTTP API with a Cognito JWT authorizer. Sign-in uses the Cognito hosted UI with the authorization code + PKCE flow, and an `organizers` group carries the organizer role.
- **Backend:** one Python 3.12 Lambda using boto3, with route dispatch, input validation, and all data access in `backend/app.py`.
- **Data:** a single DynamoDB table with three global secondary indexes for status, per-user registrations, and per-organizer events. Every read goes through a pagination helper that follows `LastEvaluatedKey`, so results are not truncated at DynamoDB's 1 MB page boundary.
- **Atomic registration:** `TransactWriteItems` combines a conditional `Put` of the registration row with a conditional `Update` of the event row. The update requires the event to be published, the seat count to be below capacity, and the deadline to be in the future, and it increments the counter in the same transaction. Cancelling a registration flips the row and decrements the counter together, so the two cannot drift apart.
- **Ownership:** organizer updates and cancellations carry a DynamoDB `ConditionExpression` on `organizerId`. Frontend role checks are display-only; the backend is the authority.

## Key Features

- Event discovery with search by title or description and filtering by category.
- Event detail views with seats remaining and the registration deadline.
- Cognito sign-up, sign-in, sign-out, and token refresh through the hosted UI.
- Atomic event registration, cancellation, and a "My Registrations" view.
- Organizer dashboard listing only the events the signed-in organizer owns, across draft, published, and cancelled states.
- Event creation with capacity and deadline validation, event cancellation, and attendee lists with live counts.
- An idempotent seed endpoint that loads reproducible sample events shared with the test suite.
- Explicit loading, empty, error, and full states in the UI.

## Technology Stack

React 19, TypeScript, and Vite provide the SPA. Amazon Cognito handles authentication and the organizer group, Amazon API Gateway exposes the HTTP API with a JWT authorizer, and AWS Lambda runs the Python backend. Amazon DynamoDB stores events and registrations, Amazon S3 and Amazon CloudFront host the frontend, and AWS CloudFormation defines the stack. Deployment is driven by the AWS CLI through a PowerShell script rather than SAM, the Serverless Framework, or Amplify.

## Implementation Highlights

- A single-table DynamoDB design keeps event and registration access patterns explicit through GSI1 (status), GSI2 (user registrations), and GSI3 (organizer events).
- Deadlines are normalized to fixed-width UTC ISO-8601 strings so DynamoDB conditional expressions can compare them lexicographically.
- Registration handles the DynamoDB rule that forbids two operations on the same item in one transaction by using one operation per item: a conditional put for the registration and a conditional update for the event.
- The infrastructure template provisions the DynamoDB table, Cognito user pool, group, and app client, the HTTP API and Lambda, and the private S3 bucket with CloudFront and an origin access control.
- `scripts/deploy.ps1` performs an idempotent deployment: package the backend, deploy the stack, ensure the hosted-UI domain, write the frontend environment, build and upload the SPA, tighten CORS to the CloudFront origin, and smoke-test the API.
- The automated tests include an in-memory DynamoDB fake that reproduces the transaction rules the original registration flow violated, which makes the concurrent-capacity test meaningful.

## Results and Validation

- `tests/test_backend.py` contains 59 backend unit tests that run without AWS calls, covering validation, deadlines, organizer checks, ownership, pagination, seeding, and registration behavior.
- `tests/e2e_live.py` exercises a deployed stack end to end, including real Cognito users, real API calls, a concurrent-registration race, and cleanup.
- The frontend is checked with `tsc -b` and `vite build`, and linted with oxlint.
- A live deployment is available at the linked demo URL, where students can sign up and organizers can be promoted with the documented Cognito admin command.

## Limitations and Future Improvements

- The scope is intentionally payment-free: prices are informational and there is no checkout flow.
- Organizer promotion is an administrative Cognito command, not a self-service workflow.
- The stack is deployed to a single AWS region and is sized for a low-cost, free-tier-oriented demo rather than high availability across regions.
- Tests and deployment are run from the repository scripts; there is no CI workflow in the repository that runs them automatically.
