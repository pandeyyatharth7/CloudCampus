"""End-to-end verification against the deployed CloudCampus API.

Unlike test_backend.py this talks to real AWS. It creates throwaway Cognito
users in the project's user pool, exercises the full authenticated flow against
the live API, prints the evidence, then removes the users again.

    py -3.11 tests/e2e_live.py [--profile opencode_Agent] [--region ap-south-1]

Requires an authenticated AWS session (aws login) and read access to Cognito.
No email is sent: users are created with DesiredDeliveryMediums=[] and an
explicitly verified email address.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import boto3

PASS = []
FAIL = []


def check(name, condition, detail=''):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f'  {detail}' if detail else ''))
    return condition


def call(base, path, token=None, method='GET', payload=None):
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or '{}')
        except json.JSONDecodeError:
            return e.code, {'raw': raw}
    except urllib.error.URLError as e:
        return 0, {'error': str(e)}


def make_user(cognito, pool_id, email, password):
    """Create a confirmed user without sending any email.
    Idempotent: if the user already exists, just sets the password and signs in
    so the returned token reflects current group membership."""
    try:
        cognito.admin_create_user(
            UserPoolId=pool_id, Username=email,
            UserAttributes=[{'Name': 'email', 'Value': email},
                            {'Name': 'email_verified', 'Value': 'true'}],
            DesiredDeliveryMediums=[],
            TemporaryPassword=password, MessageAction='SUPPRESS',
        )
    except cognito.exceptions.UsernameExistsException:
        pass
    cognito.admin_set_user_password(UserPoolId=pool_id, Username=email,
                                    Password=password, Permanent=True)
    resp = cognito.admin_initiate_auth(
        UserPoolId=pool_id, ClientId=CLIENT_ID,
        AuthFlow='ADMIN_USER_PASSWORD_AUTH',
        AuthParameters={'USERNAME': email, 'PASSWORD': password},
    )
    return resp['AuthenticationResult']['AccessToken']


def describe(token):
    claims = json.loads(urllib.request.urlopen(
        urllib.request.Request(token, method='GET')).read() or '{}')
    return claims


def main():
    global CLIENT_ID
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', default='opencode_Agent')
    ap.add_argument('--region', default='ap-south-1')
    ap.add_argument('--stack', default='cloudcampus-prod')
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cfn = session.client('cloudformation')
    outputs = {o['OutputKey']: o['OutputValue']
               for o in cfn.describe_stacks(StackName=args.stack)['Stacks'][0]['Outputs']}
    base = outputs['ApiUrl']
    pool_id = outputs['UserPoolId']
    CLIENT_ID = outputs['UserPoolClientId']
    print(f'API      : {base}')
    print(f'UserPool : {pool_id}')
    print(f'Client   : {CLIENT_ID}\n')

    cognito = session.client('cognito-idp')
    stamp = uuid.uuid4().hex[:8]
    pwd = 'CloudCampus!' + uuid.uuid4().hex[:8] + 'aA1'
    org_email = f'e2e-organizer-{stamp}@example.test'
    stu_email = f'e2e-student-{stamp}@example.test'
    other_email = f'e2e-other-{stamp}@example.test'

    org = stu = other = None
    created_events = []
    try:
        print('1. identity / anonymous surface')
        status, body = call(base, '/health')
        check('GET /health returns 200', status == 200, f'status={status}')
        status, _ = call(base, '/events')
        check('GET /events is public', status == 200, f'status={status}')
        status, _ = call(base, '/events', method='POST', payload={})
        check('POST /events without a token is 401', status == 401, f'status={status}')
        status, _ = call(base, '/me/registrations')
        check('GET /me/registrations without a token is 401', status == 401, f'status={status}')

        print('\n2. seed sample data (organizer only)')
        org = make_user(cognito, pool_id, org_email, pwd)
        cognito.admin_add_user_to_group(UserPoolId=pool_id, Username=org_email,
                                        GroupName='organizers')
        # group membership is only minted into the token at next sign-in
        org = make_user(cognito, pool_id, org_email, pwd)
        status, body = call(base, '/seed', token=org, method='POST', payload={})
        check('POST /seed succeeds for an organizer', status == 201, f'status={status} {body.get("message")}')
        first = len(body.get('data', {}).get('created', [])) if status == 201 else -1
        status, body = call(base, '/seed', token=org, method='POST', payload={})
        second = len(body.get('data', {}).get('created', [])) if status == 201 else -1
        check('re-seeding creates nothing new', status == 201 and second == 0,
              f'first run created={first}, second run created={second}')
        status, body = call(base, '/events')
        published = body.get('data', [])
        check('GET /events returns seeded published events', status == 200 and len(published) >= 4,
              f'count={len(published)}')

        print('\n3. authorization boundaries')
        stu = make_user(cognito, pool_id, stu_email, pwd)
        other = make_user(cognito, pool_id, other_email, pwd)
        status, _ = call(base, '/events', token=stu, method='POST', payload={})
        check('student cannot create events (403)', status == 403, f'status={status}')
        status, _ = call(base, '/seed', token=stu, method='POST', payload={})
        check('student cannot seed (403)', status == 403, f'status={status}')

        print('\n4. organizer creates an event')
        deadline = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat()
        new_event = {
            'title': f'E2E Fictional Event {stamp}',
            'description': 'Created by tests/e2e_live.py to verify the deployed flow.',
            'category': 'E2E', 'date': '2027-02-01', 'time': '10:00',
            'location': 'Test Hall', 'capacity': 1, 'deadline': deadline,
        }
        status, body = call(base, '/events', token=org, method='POST', payload=new_event)
        check('organizer can create an event', status == 201, f'status={status} {body.get("message")}')
        eid = body.get('data', {}).get('id')
        created_events.append(eid)

        print('\n5. owner-only mutations')
        status, _ = call(base, f'/events/{eid}/cancel', token=other, method='POST')
        check('non-owner cannot cancel (403)', status == 403, f'status={status}')
        status, _ = call(base, f'/events/{eid}/attendees', token=stu)
        check('student cannot read attendees (403)', status == 403, f'status={status}')
        status, _ = call(base, f"/events/{eid}", method='GET')
        check('GET /events/{id} is public', status == 200, f'status={status}')

        print('\n6. registration flow')
        status, body = call(base, f'/events/{eid}/register', token=stu, method='POST')
        check('student can register', status == 201, f'status={status} {body.get("message")}')
        status, body = call(base, f'/events/{eid}/register', token=stu, method='POST')
        check('duplicate registration rejected (409)', status == 409, f'status={status} {body.get("message")}')
        status, body = call(base, f'/events/{eid}', method='GET')
        check('seat counter incremented', body.get('data', {}).get('registrations') == 1,
              f"registrations={body.get('data', {}).get('registrations')}")
        status, body = call(base, '/me/registrations', token=stu)
        rows = body.get('data', [])
        check('registration appears in /me/registrations', status == 200 and any(
            r['registration']['eventId'] == eid for r in rows), f'rows={len(rows)}')

        print('\n7. capacity enforcement')
        status, body = call(base, f'/events/{eid}/register', token=other, method='POST')
        check('full event rejects a new registration (409)', status == 409,
              f'status={status} {body.get("message")}')
        status, body = call(base, f'/events/{eid}')
        check('full event shows zero available seats',
              body.get('data', {}).get('availableSeats') == 0,
              f"availableSeats={body.get('data', {}).get('availableSeats')}")

        print('\n8. concurrent registration never over-books')
        race_id = None
        race = {
            'title': f'E2E Race {stamp}', 'description': 'Concurrent registration probe.',
            'category': 'E2E', 'date': '2027-03-01', 'time': '10:00',
            'location': 'Test Hall', 'capacity': 3, 'deadline': deadline,
        }
        status, body = call(base, '/events', token=org, method='POST', payload=race)
        race_id = body.get('data', {}).get('id')
        created_events.append(race_id)
        if status == 201:
            import concurrent.futures
            tokens = []
            for i in range(8):
                email = f'e2e-race-{i}-{stamp}@example.test'
                tokens.append((make_user(cognito, pool_id, email, pwd), email))
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                codes = list(pool.map(
                    lambda t: call(base, f'/events/{race_id}/register', token=t[0], method='POST')[0],
                    tokens))
            for _, email in tokens:
                cognito.admin_delete_user(UserPoolId=pool_id, Username=email)
            created = codes.count(201)
            check('exactly 3 of 8 concurrent registrations succeed',
                  created == 3 and all(c in (201, 409) for c in codes),
                  f'codes={sorted(codes)}')
            status, body = call(base, f'/events/{race_id}')
            check('counter equals capacity after the race',
                  body.get('data', {}).get('registrations') == 3,
                  f"registrations={body.get('data', {}).get('registrations')}")

        print('\n9. cancellation')
        status, body = call(base, f'/events/{eid}/unregister', token=stu, method='POST')
        check('student can cancel their registration', status == 200, f'status={status}')
        status, body = call(base, f'/events/{eid}/unregister', token=stu, method='POST')
        check('second cancellation rejected (409)', status == 409, f'status={status} {body.get("message")}')
        status, body = call(base, f'/events/{eid}')
        check('counter decremented back to 0',
              body.get('data', {}).get('registrations') == 0,
              f"registrations={body.get('data', {}).get('registrations')}")
        status, body = call(base, f'/events/{eid}/register', token=stu, method='POST')
        check('re-registration after cancelling works', status == 201, f'status={status}')
        status, body = call(base, f'/events/{eid}/unregister', token=stu, method='POST')

        print('\n10. organizer views')
        status, body = call(base, '/me/events', token=org)
        mine = body.get('data', [])
        # Organizer sees their own events (seed data belongs to prior runs'
        # organizers, so only probe events created in this run are visible).
        check('organizer sees own events across statuses', status == 200 and len(mine) >= 2,
              f'count={len(mine)}')
        check('organizer sees at least one published event',
              any(e['status'] == 'published' for e in mine))
        status, body = call(base, f'/events/{eid}/attendees', token=org)
        check('owner can read attendees', status == 200 and body['data']['count'] == 0,
              f"count={body.get('data', {}).get('count')}")

        print('\n11. error handling')
        status, body = call(base, '/events', token=org, method='POST', payload={'title': 'x'})
        check('invalid event payload rejected with details', status == 400 and 'details' in body,
              f'status={status}')
        status, _ = call(base, '/events/does-not-exist')
        check('missing event returns 404', status == 404, f'status={status}')
        status, _ = call(base, '/nope')
        check('unknown route returns 404', status == 404, f'status={status}')
    finally:
        print('\n12. cleanup')
        # remove the probe events created by this run (seed data is left in place)
        removed = 0
        for eid in created_events:
            if not eid:
                continue
            code, _ = call(base, f'/events/{eid}/cancel', token=org, method='POST')
            if code in (200, 409):
                removed += 1
        for email in (org_email, stu_email, other_email):
            try:
                cognito.admin_delete_user(UserPoolId=pool_id, Username=email)
                print(f'  deleted user {email}')
            except Exception as e:  # noqa: BLE001
                print(f'  could not delete {email}: {e}')
        print(f'  cancelled {removed} probe event(s); seeded sample data left in place')

    print(f'\n==== {len(PASS)} passed, {len(FAIL)} failed ====')
    for f in FAIL:
        print('  FAILED:', f)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())