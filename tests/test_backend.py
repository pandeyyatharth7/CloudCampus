"""Backend unit tests (stdlib unittest + the in-memory DynamoDB fake in fakes.py).

No AWS calls are made. Run with:  py -3.11 tests/test_backend.py
"""
import json
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import app  # noqa: E402
import fakes  # noqa: E402
import seed_data  # noqa: E402


def make_event(method='GET', path='/health', body=None, claims=None, query=None):
    return {
        'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': claims or {}}}},
        'rawPath': path,
        'body': body,
        'isBase64Encoded': False,
        'queryStringParameters': query,
    }


def body_of(response):
    return json.loads(response['body'])


STUDENT = {'sub': 'user-student', 'email': 'student@example.test', 'cognito:groups': 'students'}
STUDENT2 = {'sub': 'user-other', 'email': 'other@example.test', 'cognito:groups': 'students'}
ORGANIZER = {'sub': 'user-org', 'email': 'org@example.test', 'name': 'Fictional Organizer',
             'cognito:groups': 'organizers'}


def future_iso(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def event_body(**overrides):
    payload = {
        'title': 'Fictional Test Event',
        'description': 'A description that is long enough to pass validation.',
        'category': 'Tech',
        'date': '2026-12-01',
        'time': '09:00',
        'location': 'Hall A',
        'capacity': 2,
        'deadline': future_iso(),
    }
    payload.update(overrides)
    return payload


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.table = fakes.install(app)

    def tearDown(self):
        fakes.uninstall(app)

    def create_event(self, claims=ORGANIZER, **overrides):
        response = app.handler(make_event('POST', '/events', json.dumps(event_body(**overrides)), claims))
        self.assertEqual(response['statusCode'], 201, response['body'])
        return body_of(response)['data']

    def register(self, event_id, claims=STUDENT):
        return app.handler(make_event('POST', f'/events/{event_id}/register', None, claims))


# --------------------------------------------------------------------------
# validation / auth / routing
# --------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_valid_input_passes(self):
        errs = app.validate_event_input({
            'title': 'Career Fair', 'description': 'A great fair for students',
            'category': 'Career', 'date': '2026-12-01', 'time': '09:00',
            'location': 'Hall A', 'capacity': 100, 'deadline': '2026-11-30T18:00:00+00:00',
        })
        self.assertEqual(errs, {})

    def test_missing_fields_reported(self):
        errs = app.validate_event_input({})
        for f in ('title', 'description', 'category', 'date', 'location', 'capacity', 'deadline'):
            self.assertIn(f, errs)

    def test_bad_capacity_rejected(self):
        base = {'title': 'Abc', 'description': '0123456789', 'category': 'C',
                'date': '2026-12-01', 'location': 'L', 'deadline': '2026-11-30T18:00:00+00:00'}
        self.assertIn('capacity', app.validate_event_input({**base, 'capacity': 0}))
        self.assertIn('capacity', app.validate_event_input({**base, 'capacity': 'x'}))

    def test_bad_deadline_rejected(self):
        errs = app.validate_event_input(event_body(deadline='not-a-date'))
        self.assertIn('deadline', errs)


class DeadlineTests(unittest.TestCase):
    def test_canonical_deadline_normalises_to_utc(self):
        self.assertEqual(app.canonical_deadline('2026-11-14T23:30:00Z'), '2026-11-14T23:30:00+00:00')
        self.assertEqual(app.canonical_deadline('2026-11-15T05:00:00+05:30'), '2026-11-14T23:30:00+00:00')

    def test_canonical_deadline_sortable(self):
        # DynamoDB compares the deadline attribute as a string, so the stored
        # form has to sort chronologically.
        now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
        soon = app.canonical_deadline('2026-10-19T13:00:00+00:00')
        later = app.canonical_deadline('2026-11-01T00:00:00+00:00')
        past = app.canonical_deadline('2026-10-19T11:00:00+00:00')
        self.assertTrue(soon > now.isoformat())
        self.assertTrue(later > now.isoformat())
        self.assertFalse(past > now.isoformat())

    def test_deadline_passed(self):
        self.assertTrue(app.deadline_passed('2000-01-01T00:00:00+00:00'))
        self.assertFalse(app.deadline_passed(future_iso()))


class AuthTests(unittest.TestCase):
    def test_organizer_group_string(self):
        self.assertTrue(app.is_organizer({'cognito:groups': 'organizers'}))

    def test_organizer_group_list(self):
        self.assertTrue(app.is_organizer({'cognito:groups': ['students', 'organizers']}))

    def test_non_organizer(self):
        self.assertFalse(app.is_organizer({'cognito:groups': 'students'}))
        self.assertFalse(app.is_organizer({}))


class RouterTests(BaseCase):
    def test_health_public(self):
        r = app.handler(make_event('GET', '/health'))
        self.assertEqual(r['statusCode'], 200)

    def test_unknown_route_404(self):
        r = app.handler(make_event('GET', '/nope'))
        self.assertEqual(r['statusCode'], 404)

    def test_protected_routes_need_login(self):
        for method, path in [('POST', '/events'), ('POST', '/events/x/register'),
                             ('GET', '/me/registrations'), ('POST', '/seed'),
                             ('GET', '/me/events')]:
            r = app.handler(make_event(method, path))
            self.assertEqual(r['statusCode'], 401, f'{method} {path}')

    def test_organizer_routes_reject_students(self):
        for method, path in [('POST', '/events'), ('POST', '/seed')]:
            r = app.handler(make_event(method, path, '{}', STUDENT))
            self.assertEqual(r['statusCode'], 403, f'{method} {path}')

    def test_options_preflight_has_no_body(self):
        r = app.handler(make_event('OPTIONS', '/events'))
        self.assertEqual(r['statusCode'], 204)
        self.assertEqual(r['body'], '')

    def test_cors_origin_header(self):
        r = app.handler(make_event('GET', '/health'))
        self.assertIn('Access-Control-Allow-Origin', r['headers'])


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------

class EventTests(BaseCase):
    def test_create_sets_index_keys_and_zero_counter(self):
        ev = self.create_event()
        item = self.table.items[('EVENT#' + ev['id'], 'EVENT#' + ev['id'])]
        self.assertEqual(item['GSI1PK'], 'STATUS#published')
        self.assertEqual(item['GSI1SK'], f"DATE#2026-12-01#{ev['id']}")
        self.assertEqual(item['GSI3PK'], 'ORGANIZER#user-org')
        self.assertEqual(item['registrations'], 0)
        self.assertEqual(ev['availableSeats'], 2)

    def test_deadline_stored_canonically(self):
        ev = self.create_event(deadline='2026-11-14T23:30:00Z')
        item = self.table.items[('EVENT#' + ev['id'], 'EVENT#' + ev['id'])]
        self.assertEqual(item['deadline'], '2026-11-14T23:30:00+00:00')

    def test_invalid_json_rejected(self):
        r = app.handler(make_event('POST', '/events', 'not-json', ORGANIZER))
        self.assertEqual(r['statusCode'], 400)

    def test_validation_failure_returns_details(self):
        r = app.handler(make_event('POST', '/events', json.dumps({'title': 'x'}), ORGANIZER))
        self.assertEqual(r['statusCode'], 400)
        self.assertIn('details', body_of(r))

    def test_get_missing_event_404(self):
        r = app.handler(make_event('GET', '/events/missing'))
        self.assertEqual(r['statusCode'], 404)

    def test_only_owner_can_edit(self):
        ev = self.create_event()
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'title': 'Hijacked'}), STUDENT))
        self.assertEqual(r['statusCode'], 403)
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'title': 'Hijacked'}), STUDENT2))
        self.assertEqual(r['statusCode'], 403)

    def test_non_owner_organizer_cannot_edit(self):
        ev = self.create_event()
        other = dict(ORGANIZER, sub='user-org2')
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'title': 'Hijacked'}), other))
        self.assertEqual(r['statusCode'], 403)

    def test_owner_can_edit(self):
        ev = self.create_event()
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'title': 'Renamed Event'}), ORGANIZER))
        self.assertEqual(r['statusCode'], 200)
        self.assertEqual(body_of(r)['data']['title'], 'Renamed Event')

    def test_status_change_moves_gsi1_partition(self):
        ev = self.create_event()
        app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'status': 'draft'}), ORGANIZER))
        item = self.table.items[('EVENT#' + ev['id'], 'EVENT#' + ev['id'])]
        self.assertEqual(item['GSI1PK'], 'STATUS#draft')
        listed = body_of(app.handler(make_event('GET', '/events', query={'status': 'draft'})))['data']
        self.assertEqual([e['id'] for e in listed], [ev['id']])

    def test_date_change_updates_both_gsi_sort_keys(self):
        ev = self.create_event()
        app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'date': '2027-03-04'}), ORGANIZER))
        item = self.table.items[('EVENT#' + ev['id'], 'EVENT#' + ev['id'])]
        self.assertEqual(item['GSI1SK'], f"DATE#2027-03-04#{ev['id']}")
        self.assertEqual(item['GSI3SK'], f"DATE#2027-03-04#{ev['id']}")

    def test_capacity_cannot_drop_below_registrations(self):
        ev = self.create_event(capacity=3)
        app.handler(make_event('POST', f"/events/{ev['id']}/register", None, STUDENT))
        app.handler(make_event('POST', f"/events/{ev['id']}/register", None, STUDENT2))
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'capacity': 1}), ORGANIZER))
        self.assertEqual(r['statusCode'], 409)
        self.assertIn('Capacity cannot be lower', body_of(r)['message'])
        # lowering to exactly the number of active registrations is still fine
        r = app.handler(make_event('PATCH', f"/events/{ev['id']}", json.dumps({'capacity': 2}), ORGANIZER))
        self.assertEqual(r['statusCode'], 200)

    def test_cancel_twice_is_409(self):
        ev = self.create_event()
        self.assertEqual(app.handler(make_event('POST', f"/events/{ev['id']}/cancel", None, ORGANIZER))['statusCode'], 200)
        self.assertEqual(app.handler(make_event('POST', f"/events/{ev['id']}/cancel", None, ORGANIZER))['statusCode'], 409)

    def test_non_owner_cannot_cancel(self):
        ev = self.create_event()
        r = app.handler(make_event('POST', f"/events/{ev['id']}/cancel", None, dict(ORGANIZER, sub='x')))
        self.assertEqual(r['statusCode'], 403)

    def test_attendees_owner_only(self):
        ev = self.create_event()
        for claims in (STUDENT, dict(ORGANIZER, sub='x')):
            r = app.handler(make_event('GET', f"/events/{ev['id']}/attendees", None, claims))
            self.assertEqual(r['statusCode'], 403)

    def test_my_events_returns_own_events_only(self):
        mine = self.create_event()
        self.create_event(claims=dict(ORGANIZER, sub='user-org2'))
        listed = body_of(app.handler(make_event('GET', '/me/events', None, ORGANIZER)))['data']
        self.assertEqual([e['id'] for e in listed], [mine['id']])


# --------------------------------------------------------------------------
# registration - the behaviour that was broken
# --------------------------------------------------------------------------

class RegistrationTests(BaseCase):
    def test_register_creates_row_and_increments_counter(self):
        ev = self.create_event()
        r = self.register(ev['id'])
        self.assertEqual(r['statusCode'], 201, r['body'])
        reg = self.table.items[(f"EVENT#{ev['id']}", 'USER#user-student')]
        self.assertEqual(reg['status'], 'registered')
        self.assertEqual(self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]['registrations'], 1)
        self.assertEqual(body_of(app.handler(make_event('GET', f"/events/{ev['id']}")))['data']['availableSeats'], 1)

    def test_duplicate_registration_rejected(self):
        ev = self.create_event()
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        r = self.register(ev['id'])
        self.assertEqual(r['statusCode'], 409)
        self.assertIn('Already registered', body_of(r)['message'])
        self.assertEqual(self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]['registrations'], 1)

    def test_full_event_rejects(self):
        ev = self.create_event(capacity=1)
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        r = self.register(ev['id'], STUDENT2)
        self.assertEqual(r['statusCode'], 409)
        self.assertIn('full capacity', body_of(r)['message'])

    def test_cancelled_event_rejects(self):
        ev = self.create_event()
        app.handler(make_event('POST', f"/events/{ev['id']}/cancel", None, ORGANIZER))
        self.assertEqual(self.register(ev['id'])['statusCode'], 409)

    def test_expired_deadline_rejects(self):
        ev = self.create_event()
        key = (f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")
        self.table.items[key]['deadline'] = '2020-01-01T00:00:00+00:00'
        r = self.register(ev['id'])
        self.assertEqual(r['statusCode'], 409)
        self.assertIn('deadline', body_of(r)['message'])

    def test_unregister_then_register_again(self):
        ev = self.create_event()
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        self.assertEqual(app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))['statusCode'], 200)
        self.assertEqual(self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]['registrations'], 0)
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        self.assertEqual(self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]['registrations'], 1)

    def test_double_unregister_rejected(self):
        ev = self.create_event()
        self.register(ev['id'])
        self.assertEqual(app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))['statusCode'], 200)
        r = app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))
        self.assertEqual(r['statusCode'], 409)
        self.assertEqual(self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]['registrations'], 0)

    def test_unregister_without_registration_rejected(self):
        ev = self.create_event()
        r = app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))
        self.assertEqual(r['statusCode'], 409)

    def test_counter_never_goes_negative(self):
        ev = self.create_event()
        key = (f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")
        self.register(ev['id'])
        self.table.items[key]['registrations'] = 0  # simulate drift
        r = app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))
        self.assertEqual(r['statusCode'], 409)
        self.assertEqual(self.table.items[key]['registrations'], 0)

    def test_my_registrations_only_lists_active(self):
        ev = self.create_event()
        self.register(ev['id'])
        self.register(ev['id'], STUDENT2)
        app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT2))
        rows = body_of(app.handler(make_event('GET', '/me/registrations', None, STUDENT)))['data']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['registration']['userId'], 'user-student')
        self.assertEqual(rows[0]['event']['id'], ev['id'])

    def test_no_user_registered_twice_under_parallel_load(self):
        """Capacity must never be over-booked, even with concurrent callers."""
        ev = self.create_event(capacity=3)
        students = [{'sub': f'user-{i}', 'email': f'u{i}@example.test', 'cognito:groups': 'students'}
                    for i in range(12)]

        def attempt(claims):
            return self.register(ev['id'], claims)['statusCode']

        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(attempt, students))

        created = codes.count(201)
        self.assertEqual(created, 3, f'expected exactly 3 seats taken, got {created}')
        self.assertTrue(all(c in (201, 409) for c in codes))
        item = self.table.items[(f"EVENT#{ev['id']}", f"EVENT#{ev['id']}")]
        self.assertEqual(item['registrations'], 3)
        rows = [i for k, i in self.table.items.items() if k[0] == f"EVENT#{ev['id']}" and k[1].startswith('USER#')]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['userId'] for r in rows}), 3)


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------

class SeedDataTests(unittest.TestCase):
    def test_spec_is_consistent(self):
        self.assertTrue(seed_data.SEED_EVENTS)
        ids = [s['id'] for s in seed_data.SEED_EVENTS]
        self.assertEqual(len(ids), len(set(ids)), 'seed ids must be unique')
        for spec in seed_data.SEED_EVENTS:
            self.assertTrue(spec['id'].startswith(seed_data.SEED_ID_PREFIX))
            self.assertLessEqual(spec['attendees'], spec['capacity'])
            self.assertIn(spec['status'], ('published', 'draft', 'cancelled'))

    def test_deadline_precedes_event_and_is_sortable(self):
        deadline = seed_data.deadline_for('2026-11-15')
        self.assertEqual(deadline, '2026-11-14T18:00:00+00:00')
        # canonical UTC form, and strictly earlier than the event's own date
        self.assertEqual(app.canonical_deadline(deadline), deadline)
        self.assertLess(deadline, '2026-11-15T00:00:00+00:00')
        self.assertGreater(seed_data.deadline_for('2026-11-16'), deadline)

    def test_covers_mixed_categories_and_states(self):
        categories = {s['category'] for s in seed_data.SEED_EVENTS}
        statuses = {s['status'] for s in seed_data.SEED_EVENTS}
        self.assertGreaterEqual(len(categories), 4)
        self.assertEqual(statuses, {'published', 'draft', 'cancelled'})
        self.assertTrue(any(s['attendees'] == s['capacity'] for s in seed_data.SEED_EVENTS), 'want a full event')
        self.assertTrue(any(s['attendees'] == 0 for s in seed_data.SEED_EVENTS), 'want an empty event')


class SeedTests(BaseCase):
    def seed(self, claims=ORGANIZER, payload=None):
        return app.handler(make_event('POST', '/seed', json.dumps(payload or {}), claims))

    def test_seed_creates_events_and_registrations(self):
        r = self.seed()
        self.assertEqual(r['statusCode'], 201, r['body'])
        data = body_of(r)['data']
        self.assertEqual(len(data['created']), len(seed_data.SEED_EVENTS))
        self.assertEqual(data['skipped'], [])

    def test_seed_is_idempotent(self):
        self.seed()
        before = len(self.table.items)
        r = self.seed()
        self.assertEqual(r['statusCode'], 201)
        data = body_of(r)['data']
        self.assertEqual(data['created'], [])
        self.assertEqual(len(data['skipped']), len(seed_data.SEED_EVENTS))
        self.assertEqual(len(self.table.items), before, 're-seeding must not add rows')

    def test_seeded_counter_matches_registration_rows(self):
        self.seed()
        for spec in seed_data.SEED_EVENTS:
            key = (f"EVENT#{spec['id']}", f"EVENT#{spec['id']}")
            rows = [i for k, i in self.table.items.items()
                    if k[0] == f"EVENT#{spec['id']}" and k[1].startswith('USER#') and i['status'] == 'registered']
            self.assertEqual(self.table.items[key]['registrations'], len(rows), spec['id'])
            self.assertLessEqual(len(rows), spec['capacity'], spec['id'])

    def test_seed_marks_rows_synthetic(self):
        self.seed()
        key = (f"EVENT#seed-career-fair-2026", f"EVENT#seed-career-fair-2026")
        self.assertTrue(self.table.items[key]['synthetic'])
        reg_key = (f"EVENT#seed-career-fair-2026", f"USER#{seed_data.attendee_id(1)}")
        self.assertTrue(self.table.items[reg_key]['synthetic'])
        self.assertTrue(seed_data.attendee_id(1).startswith(seed_data.SEED_ATTENDEE_PREFIX))
        self.assertNotIn(seed_data.attendee_id(1), {'seed-attendee', 'attendee-01'})

    def test_reset_refuses_when_real_registrations_exist(self):
        self.seed()
        ev = self.create_event(title='Real Organizer Event', capacity=5)
        app.handler(make_event('POST', f"/events/{ev['id']}/register", None, STUDENT))
        # link a live registration onto a seed event too
        self.table.items[('EVENT#seed-career-fair-2026', 'USER#user-student')] = {
            'PK': 'EVENT#seed-career-fair-2026', 'SK': 'USER#user-student',
            'GSI2PK': 'USER#user-student', 'GSI2SK': 'EVENT#seed-career-fair-2026',
            'eventId': 'seed-career-fair-2026', 'userId': 'user-student', 'status': 'registered',
        }
        r = self.seed(payload={'reset': True})
        self.assertEqual(r['statusCode'], 409)
        self.assertIn('Refusing to reset', body_of(r)['message'])

    def test_reset_recounts_without_losing_owner(self):
        self.seed()
        app.handler(make_event('POST', f"/events/{'seed-career-fair-2026'}/register", None, STUDENT))
        r = self.seed(payload={'reset': True})
        self.assertEqual(r['statusCode'], 409)
        key = ('EVENT#seed-career-fair-2026', 'EVENT#seed-career-fair-2026')
        self.assertEqual(self.table.items[key]['organizerId'], 'user-org')

    def test_seeded_events_are_owned_by_caller(self):
        self.seed()
        listed = body_of(app.handler(make_event('GET', '/me/events', None, ORGANIZER)))['data']
        self.assertEqual(len(listed), len(seed_data.SEED_EVENTS))

    def test_bad_seed_body_rejected(self):
        r = app.handler(make_event('POST', '/seed', 'not-json', ORGANIZER))
        self.assertEqual(r['statusCode'], 400)


class TransactionShapeTests(BaseCase):
    """Regression guards for the bug the Cline handover shipped.

    The old register handler sent four operations touching only two items, plus
    an ExpressionAttributeValues entry nothing referenced. DynamoDB rejects both
    shapes outright, so registration could never succeed. The fake enforces the
    same rules; these tests pin the shape so a future refactor cannot reintroduce it.
    """

    def spy_on_transactions(self):
        """Wrap transact_write_items so the request payload can be inspected."""
        captured = {}
        real = self.table.client.transact_write_items

        def spy(**kw):
            captured.update(kw)
            return real(**kw)

        self.table.client.transact_write_items = spy
        return captured

    @staticmethod
    def touched_items(TransactItems):
        keys = []
        for op in TransactItems:
            _, body = next(iter(op.items()))
            if 'Item' in body:
                keys.append((body['Item']['PK']['S'], body['Item']['SK']['S']))
            else:
                keys.append((body['Key']['PK']['S'], body['Key']['SK']['S']))
        return keys

    def test_register_transaction_touches_each_item_once(self):
        ev = self.create_event()
        captured = self.spy_on_transactions()
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        items = captured['TransactItems']
        keys = self.touched_items(items)
        self.assertEqual(len(keys), len(set(keys)), f'two operations on one item: {items}')
        self.assertEqual(len(items), 2)

    def test_register_transaction_has_no_unused_expression_values(self):
        ev = self.create_event()
        captured = self.spy_on_transactions()
        self.assertEqual(self.register(ev['id'])['statusCode'], 201)
        for op in captured['TransactItems']:
            name, body = next(iter(op.items()))
            joined = ' '.join(filter(None, [body.get('ConditionExpression'), body.get('UpdateExpression')]))
            for k in body.get('ExpressionAttributeValues', {}):
                self.assertIn(k, joined, f'unused value :{k} in {name}')

    def test_unregister_transaction_touches_each_item_once(self):
        ev = self.create_event()
        self.register(ev['id'])
        captured = self.spy_on_transactions()
        r = app.handler(make_event('POST', f"/events/{ev['id']}/unregister", None, STUDENT))
        self.assertEqual(r['statusCode'], 200)
        keys = self.touched_items(captured['TransactItems'])
        self.assertEqual(len(keys), len(set(keys)), f'two operations on one item: {captured}')


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------

class PaginationTests(BaseCase):
    def test_query_all_follows_pages(self):
        self.table.page_size = 2
        for i in range(5):
            self.create_event(title=f'Paged Event {i}')
        listed = body_of(app.handler(make_event('GET', '/events')))['data']
        self.assertEqual(len(listed), 5)

    def test_my_registrations_follows_pages(self):
        self.table.page_size = 1
        for i in range(3):
            ev = self.create_event(title=f'Multi Event {i}', capacity=5)
            app.handler(make_event('POST', f"/events/{ev['id']}/register", None, STUDENT))
        rows = body_of(app.handler(make_event('GET', '/me/registrations', None, STUDENT)))['data']
        self.assertEqual(len(rows), 3)

    def test_attendees_follows_pages(self):
        ev = self.create_event(capacity=10)
        for i in range(5):
            app.handler(make_event('POST', f"/events/{ev['id']}/register", None,
                                   {'sub': f'user-{i}', 'cognito:groups': 'students'}))
        data = body_of(app.handler(make_event('GET', f"/events/{ev['id']}/attendees", None, ORGANIZER)))['data']
        self.assertEqual(data['count'], 5)
        self.assertEqual(len(data['attendees']), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)