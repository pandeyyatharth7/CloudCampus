"""CloudCampus single-Lambda backend (Python 3.12, boto3 only).

Routes (API Gateway HTTP API, proxy integration):
  GET    /events
  GET    /events/{id}
  POST   /events                  (organizer)
  PATCH  /events/{id}             (organizer, owner)
  POST   /events/{id}/cancel      (organizer, owner)
  POST   /events/{id}/register    (auth)
  POST   /events/{id}/unregister  (auth)
  GET    /me/registrations        (auth)
  GET    /me/events               (auth, organizer: events I own)
  GET    /events/{id}/attendees   (organizer, owner)
  POST   /seed                    (organizer; reproducible sample data)
  GET    /health                  (public)

Auth: API Gateway JWT authorizer (Cognito). Claims arrive in
event['requestContext']['authorizer']['jwt']['claims'].
Organizer check uses cognito:groups claim containing 'organizers'.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from seed_data import SEED_ATTENDEE_PREFIX, SEED_EVENTS, attendee_id, deadline_for, max_attendees

TABLE = os.environ.get('TABLE_NAME', 'cloudcampus-events')
ORGANIZER_GROUP = 'organizers'
SEED_ORGANIZER = 'cloudcampus-seed'

dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'ap-south-1'))
table = dynamodb.Table(TABLE)


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    """Parse an ISO-8601 string into an aware datetime, or None if unparseable."""
    if value in (None, ''):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def canonical_deadline(value):
    """Normalise a deadline to `YYYY-MM-DDTHH:MM:SS+00:00`.

    The DynamoDB condition `deadline > :now` is a string comparison, so stored
    deadlines must always be UTC with a fixed-width offset.
    """
    dt = parse_dt(value)
    if dt is None:
        return str(value)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def deadline_passed(value, now=None):
    """True when the registration deadline is in the past."""
    dt = parse_dt(value)
    if dt is None:
        return False
    return dt <= (now or datetime.now(timezone.utc))


def query_all(kwargs):
    """Run a table/index query, following LastEvaluatedKey to the end.

    Every read path goes through here so nothing is silently truncated at the
    1 MB DynamoDB page boundary.
    """
    items = []
    while True:
        r = table.query(**kwargs)
        items.extend(r.get('Items', []))
        start_key = r.get('LastEvaluatedKey')
        if not start_key:
            return items
        kwargs['ExclusiveStartKey'] = start_key


def to_jsonable(obj):
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, list):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def resp(status, body):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', '*'),
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        },
        'body': json.dumps(to_jsonable(body)),
    }


def ok(data=None, message=None):
    return resp(200, {'success': True, 'data': data, 'message': message})


def created(data=None, message=None):
    return resp(201, {'success': True, 'data': data, 'message': message})


def err(status, error, message=None):
    return resp(status, {'success': False, 'error': error, 'message': message or error})


def preflight():
    """CORS preflight answer: 204 with no body (RFC 9110)."""
    return {
        'statusCode': 204,
        'headers': {
            'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', '*'),
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        },
        'body': '',
    }


def get_claims(event):
    try:
        return event.get('requestContext', {}).get('authorizer', {}).get('jwt', {}).get('claims', {}) or {}
    except Exception:
        return {}


def user_id_of(claims):
    return claims.get('sub')


def is_organizer(claims):
    groups = claims.get('cognito:groups', '')
    if isinstance(groups, list):
        return ORGANIZER_GROUP in groups
    if isinstance(groups, str):
        # API Gateway HTTP API may pass cognito:groups as a string like
        # '[organizers]' (Python repr of a list) instead of a JSON array.
        cleaned = groups.strip('[]').strip('"')
        return ORGANIZER_GROUP in [g.strip().strip('"') for g in cleaned.split(',') if g.strip()]
    return False


def parse_body(event):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def public_event(item):
    return {
        'id': item.get('id'),
        'title': item.get('title'),
        'description': item.get('description'),
        'category': item.get('category'),
        'date': item.get('date'),
        'time': item.get('time'),
        'location': item.get('location'),
        'capacity': to_jsonable(item.get('capacity', 0)),
        'deadline': item.get('deadline'),
        'organizerName': item.get('organizerName'),
        'registrations': to_jsonable(item.get('registrations', 0)),
        'status': item.get('status'),
        'availableSeats': max(0, int(item.get('capacity', 0)) - int(item.get('registrations', 0))),
    }


def validate_event_input(data, partial=False):
    errors = {}

    def need(field):
        if field not in data or data[field] in (None, ''):
            errors[field] = 'required'

    fields = ['title', 'description', 'category', 'date', 'time', 'location', 'capacity', 'deadline']
    if not partial:
        for f in fields:
            need(f)
    if 'title' in data and data.get('title') is not None and len(str(data['title']).strip()) < 3:
        errors['title'] = 'must be at least 3 characters'
    if 'description' in data and data.get('description') is not None and len(str(data['description']).strip()) < 10:
        errors['description'] = 'must be at least 10 characters'
    if 'capacity' in data and data.get('capacity') is not None:
        try:
            cap = int(data['capacity'])
            if cap < 1 or cap > 10000:
                errors['capacity'] = 'must be between 1 and 10000'
        except (TypeError, ValueError):
            errors['capacity'] = 'must be an integer'
    for f in ('date', 'deadline'):
        if f in data and data.get(f):
            if parse_dt(data[f]) is None:
                errors[f] = 'must be ISO-8601'
    if 'status' in data and data.get('status') not in (None, 'draft', 'published', 'cancelled'):
        errors['status'] = 'must be draft, published or cancelled'
    return errors

# ---------- events ----------

def list_events(event):
    params = event.get('queryStringParameters') or {}
    status = params.get('status', 'published')
    category = params.get('category')
    search = (params.get('search') or '').lower()
    try:
        items = query_all({'IndexName': 'GSI1', 'KeyConditionExpression': Key('GSI1PK').eq(f'STATUS#{status}')})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    out = [public_event(i) for i in items]
    if category:
        out = [e for e in out if (e.get('category') or '').lower() == category.lower()]
    if search:
        out = [e for e in out if search in (e.get('title') or '').lower() or search in (e.get('description') or '').lower()]
    out.sort(key=lambda e: (e.get('date') or '', e.get('time') or ''))
    return ok(out)


def get_event(event, event_id):
    try:
        r = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    item = r.get('Item')
    if not item:
        return err(404, 'not_found', 'Event not found')
    return ok(public_event(item))


def create_event(event, claims):
    data = parse_body(event)
    if data is None:
        return err(400, 'invalid_json', 'Request body must be a JSON object')
    errors = validate_event_input(data)
    if errors:
        return resp(400, {'success': False, 'error': 'validation_failed', 'details': errors})
    eid = str(uuid.uuid4())
    ts = now_iso()
    organizer_id = user_id_of(claims)
    item = {
        'PK': f'EVENT#{eid}', 'SK': f'EVENT#{eid}',
        'GSI1PK': 'STATUS#published', 'GSI1SK': f"DATE#{data['date']}#{eid}",
        'GSI3PK': f'ORGANIZER#{organizer_id}', 'GSI3SK': f"DATE#{data['date']}#{eid}",
        'id': eid, 'title': str(data['title']).strip(),
        'description': str(data['description']).strip(),
        'category': str(data['category']).strip(),
        'date': str(data['date']), 'time': str(data.get('time', '')),
        'location': str(data['location']).strip(),
        'capacity': int(data['capacity']),
        'deadline': canonical_deadline(data['deadline']),
        'organizerId': organizer_id,
        'organizerName': claims.get('name') or claims.get('email') or 'Organizer',
        'registrations': 0, 'status': 'published',
        'createdAt': ts, 'updatedAt': ts,
    }
    try:
        table.put_item(Item=item, ConditionExpression='attribute_not_exists(PK)')
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return err(409, 'conflict', 'Event already exists')
        return err(500, 'dynamodb_error', str(e))
    return created(public_event(item), 'Event created')


def update_event(event, claims, event_id):
    data = parse_body(event)
    if data is None:
        return err(400, 'invalid_json', 'Request body must be a JSON object')
    errors = validate_event_input(data, partial=True)
    if errors:
        return resp(400, {'success': False, 'error': 'validation_failed', 'details': errors})
    try:
        r = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    item = r.get('Item')
    if not item:
        return err(404, 'not_found', 'Event not found')
    if item.get('organizerId') != user_id_of(claims):
        return err(403, 'forbidden', 'Only the owning organizer can edit this event')
    if item.get('status') == 'cancelled':
        return err(409, 'conflict', 'Cancelled events cannot be edited')
    # capacity may never be lowered below the number of active registrations
    if 'capacity' in data and data.get('capacity') is not None and int(data['capacity']) < int(item.get('registrations', 0)):
        return err(409, 'conflict', f"Capacity cannot be lower than the {item.get('registrations', 0)} active registration(s)")
    expr, names, values = ['#u = :u'], {'#u': 'updatedAt'}, {':u': now_iso(), ':uid': user_id_of(claims)}
    for f in ('title', 'description', 'category', 'date', 'time', 'location', 'capacity', 'deadline', 'status'):
        if f in data and data[f] is not None:
            if f == 'capacity':
                v = int(data[f])
            elif f == 'deadline':
                v = canonical_deadline(data[f])
            elif isinstance(data[f], str):
                v = data[f].strip()
            else:
                v = data[f]
            expr.append(f'#{f} = :{f}')
            names[f'#{f}'] = f
            values[f':{f}'] = v
    # keep GSI sort keys consistent with the (possibly) new event date
    if 'date' in data and data.get('date'):
        expr.append('#g1 = :g1')
        names['#g1'] = 'GSI1SK'
        values[':g1'] = f"DATE#{data['date']}#{event_id}"
        expr.append('#g3 = :g3')
        names['#g3'] = 'GSI3SK'
        values[':g3'] = f"DATE#{data['date']}#{event_id}"
    # a status change must move the item to the matching GSI1 partition
    if 'status' in data and data.get('status'):
        expr.append('#g1p = :g1p')
        names['#g1p'] = 'GSI1PK'
        values[':g1p'] = f"STATUS#{data['status']}"
    try:
        r2 = table.update_item(
            Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'},
            UpdateExpression='SET ' + ', '.join(expr),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            ConditionExpression='organizerId = :uid',
            ReturnValues='ALL_NEW',
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return err(403, 'forbidden', 'Only the owning organizer can edit this event')
        return err(500, 'dynamodb_error', str(e))
    return ok(public_event(r2['Attributes']), 'Event updated')


def cancel_event(event, claims, event_id):
    try:
        r = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    item = r.get('Item')
    if not item:
        return err(404, 'not_found', 'Event not found')
    if item.get('organizerId') != user_id_of(claims):
        return err(403, 'forbidden', 'Only the owning organizer can cancel this event')
    if item.get('status') == 'cancelled':
        return err(409, 'conflict', 'Event is already cancelled')
    try:
        r2 = table.update_item(
            Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'},
            UpdateExpression='SET #s = :s, #u = :u',
            ExpressionAttributeNames={'#s': 'status', '#u': 'updatedAt'},
            ExpressionAttributeValues={':s': 'cancelled', ':u': now_iso(), ':uid': user_id_of(claims)},
            ConditionExpression='organizerId = :uid AND #s <> :s',
            ReturnValues='ALL_NEW',
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return err(409, 'conflict', 'Event already cancelled or not owned by you')
        return err(500, 'dynamodb_error', str(e))
    return ok(public_event(r2['Attributes']), 'Event cancelled')

# ---------- registrations (atomic) ----------

def _av(v):
    if isinstance(v, bool):
        return {'BOOL': v}
    if isinstance(v, int):
        return {'N': str(v)}
    if isinstance(v, float):
        return {'N': str(v)}
    return {'S': str(v)}


def register_for_event(event, claims, event_id):
    uid = user_id_of(claims)
    if not uid:
        return err(401, 'unauthorized', 'Valid login required')
    try:
        r = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    ev = r.get('Item')
    if not ev:
        return err(404, 'not_found', 'Event not found')
    if ev.get('status') != 'published':
        return err(409, 'conflict', 'Event is not open for registration')
    if deadline_passed(ev.get('deadline')):
        return err(409, 'conflict', 'Registration deadline has passed')
    ts = now_iso()
    reg = {
        'PK': f'EVENT#{event_id}', 'SK': f'USER#{uid}',
        'GSI2PK': f'USER#{uid}', 'GSI2SK': f'EVENT#{event_id}',
        'eventId': event_id, 'userId': uid, 'status': 'registered',
        'registeredAt': ts,
    }
    event_key = {'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'}
    client = boto3.client('dynamodb', region_name=os.environ.get('AWS_REGION', 'ap-south-1'))
    try:
        # DynamoDB rejects two operations on the same item inside one
        # transaction, so capacity/status/deadline are enforced by a single
        # conditional Update on the event item that also increments the
        # counter, and the duplicate guard is a conditional Put on the
        # registration item. Two operations, two distinct items.
        client.transact_write_items(TransactItems=[
            {'Put': {
                'TableName': TABLE,
                'Item': {k: _av(v) for k, v in reg.items()},
                'ConditionExpression': 'attribute_not_exists(PK) OR #s = :cancelled',
                'ExpressionAttributeNames': {'#s': 'status'},
                'ExpressionAttributeValues': {':cancelled': {'S': 'cancelled'}},
            }},
            {'Update': {
                'TableName': TABLE,
                'Key': {'PK': {'S': f'EVENT#{event_id}'}, 'SK': {'S': f'EVENT#{event_id}'}},
                'UpdateExpression': 'SET #r = #r + :one, #u = :u',
                'ConditionExpression': '#s = :published AND #r < #c AND #d > :now',
                'ExpressionAttributeNames': {'#s': 'status', '#r': 'registrations',
                                             '#c': 'capacity', '#d': 'deadline', '#u': 'updatedAt'},
                'ExpressionAttributeValues': {
                    ':one': {'N': '1'}, ':u': {'S': ts},
                    ':published': {'S': 'published'}, ':now': {'S': ts},
                },
            }},
        ])
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'TransactionCanceledException':
            dup = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'USER#{uid}'}).get('Item') or {}
            if dup.get('status') == 'registered':
                return err(409, 'conflict', 'Already registered for this event')
            cur = table.get_item(Key=event_key).get('Item') or {}
            if cur.get('status') != 'published':
                return err(409, 'conflict', 'Event is not open for registration')
            if int(cur.get('registrations', 0)) >= int(cur.get('capacity', 0)):
                return err(409, 'conflict', 'Event is at full capacity')
            return err(409, 'conflict', 'Registration deadline has passed')
        return err(500, 'dynamodb_error', str(e))
    return created({'eventId': event_id, 'status': 'registered', 'registeredAt': ts}, 'Registered')

def unregister_from_event(event, claims, event_id):
    uid = user_id_of(claims)
    if not uid:
        return err(401, 'unauthorized', 'Valid login required')
    ts = now_iso()
    client = boto3.client('dynamodb', region_name=os.environ.get('AWS_REGION', 'ap-south-1'))
    try:
        # Two distinct items, so both halves fit in one transaction: the
        # registration flips to cancelled and the counter decrements together,
        # so the two can never drift apart.
        client.transact_write_items(TransactItems=[
            {'Update': {
                'TableName': TABLE,
                'Key': {'PK': {'S': f'EVENT#{event_id}'}, 'SK': {'S': f'USER#{uid}'}},
                'UpdateExpression': 'SET #s = :cancelled, #c = :ts',
                'ConditionExpression': '#s = :registered',
                'ExpressionAttributeNames': {'#s': 'status', '#c': 'cancelledAt'},
                'ExpressionAttributeValues': {':cancelled': {'S': 'cancelled'}, ':ts': {'S': ts},
                                             ':registered': {'S': 'registered'}},
            }},
            {'Update': {
                'TableName': TABLE,
                'Key': {'PK': {'S': f'EVENT#{event_id}'}, 'SK': {'S': f'EVENT#{event_id}'}},
                'UpdateExpression': 'SET #r = #r - :one, #u = :u',
                'ConditionExpression': '#r > :zero',
                'ExpressionAttributeNames': {'#r': 'registrations', '#u': 'updatedAt'},
                'ExpressionAttributeValues': {':one': {'N': '1'}, ':zero': {'N': '0'}, ':u': {'S': ts}},
            }},
        ])
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'TransactionCanceledException':
            existing = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'USER#{uid}'}).get('Item') or {}
            if existing.get('status') != 'registered':
                return err(409, 'conflict', 'No active registration to cancel')
            return err(409, 'conflict', 'Registration counter is out of sync; contact an organizer')
        return err(500, 'dynamodb_error', str(e))
    return ok({'eventId': event_id, 'status': 'cancelled'}, 'Registration cancelled')


def my_registrations(event, claims):
    uid = user_id_of(claims)
    if not uid:
        return err(401, 'unauthorized', 'Valid login required')
    try:
        regs = query_all({'IndexName': 'GSI2', 'KeyConditionExpression': Key('GSI2PK').eq(f'USER#{uid}')})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    out = []
    for reg in [x for x in regs if x.get('status') == 'registered']:
        eid = reg.get('eventId')
        try:
            ev = table.get_item(Key={'PK': f'EVENT#{eid}', 'SK': f'EVENT#{eid}'}).get('Item')
        except ClientError:
            ev = None
        out.append({'registration': to_jsonable(reg), 'event': public_event(ev) if ev else None})
    out.sort(key=lambda r: ((r.get('event') or {}).get('date') or '', (r.get('event') or {}).get('time') or ''))
    return ok(out)


def my_events(event, claims):
    """Events owned by the caller across every status (organizer dashboard)."""
    uid = user_id_of(claims)
    if not uid:
        return err(401, 'unauthorized', 'Valid login required')
    try:
        items = query_all({'IndexName': 'GSI3', 'KeyConditionExpression': Key('GSI3PK').eq(f'ORGANIZER#{uid}')})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    out = [public_event(i) for i in items]
    out.sort(key=lambda e: (e.get('date') or '', e.get('time') or ''))
    return ok(out)


def event_attendees(event, claims, event_id):
    try:
        r = table.get_item(Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'})
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    ev = r.get('Item')
    if not ev:
        return err(404, 'not_found', 'Event not found')
    if ev.get('organizerId') != user_id_of(claims):
        return err(403, 'forbidden', 'Only the owning organizer can view attendees')
    try:
        regs = [to_jsonable(x) for x in registration_rows(event_id)]
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    active = [x for x in regs if x.get('status') == 'registered']
    return ok({'event': public_event(ev), 'count': len(active), 'attendees': regs})

# ---------- seed ----------


def registration_rows(event_id):
    """Every registration row for an event, following all query pages."""
    return [r for r in query_all({'KeyConditionExpression': Key('PK').eq(f'EVENT#{event_id}')})
            if str(r.get('SK', '')).startswith('USER#')]


def non_seed_registrations(event_id):
    """Active registrations made by real users (not synthetic seed attendees)."""
    return [r for r in registration_rows(event_id)
            if r.get('status') == 'registered' and not str(r.get('userId', '')).startswith(SEED_ATTENDEE_PREFIX)]


def recount_registrations(event_id, ts):
    """Make the denormalised counter match the actual active registration rows."""
    active = len([r for r in registration_rows(event_id) if r.get('status') == 'registered'])
    table.update_item(
        Key={'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'},
        UpdateExpression='SET #r = :n, #u = :u',
        ExpressionAttributeNames={'#r': 'registrations', '#u': 'updatedAt'},
        ExpressionAttributeValues={':n': active, ':u': ts},
    )
    return active


def write_seed_registrations(spec, ts):
    """Write the synthetic attendee rows for one seed event (insert only)."""
    event_id = spec['id']
    for i in range(1, max_attendees(spec) + 1):
        uid = attendee_id(i)
        is_active = i <= spec.get('attendees', 0)
        item = {
            'PK': f'EVENT#{event_id}', 'SK': f'USER#{uid}',
            'GSI2PK': f'USER#{uid}', 'GSI2SK': f'EVENT#{event_id}',
            'eventId': event_id, 'userId': uid,
            'seed': True, 'synthetic': True,
            'status': 'registered' if is_active else 'cancelled',
            'registeredAt': ts,
        }
        if not is_active:
            item['cancelledAt'] = ts
        table.put_item(Item=item, ConditionExpression='attribute_not_exists(PK)')


def seed_event_item(spec, organizer_id, organizer_name, ts):
    event_id = spec['id']
    return {
        'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}',
        'GSI1PK': f"STATUS#{spec['status']}", 'GSI1SK': f"DATE#{spec['date']}#{event_id}",
        'GSI3PK': f'ORGANIZER#{organizer_id}', 'GSI3SK': f"DATE#{spec['date']}#{event_id}",
        'id': event_id, 'title': spec['title'], 'description': spec['description'],
        'category': spec['category'], 'date': spec['date'], 'time': spec['time'],
        'location': spec['location'], 'capacity': spec['capacity'],
        'deadline': deadline_for(spec['date']),
        'organizerId': organizer_id, 'organizerName': organizer_name,
        'registrations': spec.get('attendees', 0), 'status': spec['status'],
        'seed': True, 'synthetic': True,
        'createdAt': ts, 'updatedAt': ts,
    }


def seed_events(event, claims):
    """Idempotent sample-data loader.

    Default             creates only the seed events that are missing, so
                        re-running never duplicates. Never deletes anything.
    ``{"reset": true}`` rewrites the seed rows in place (same stable ids) and
                        refuses outright if any real user registration exists on
                        a target event, so a bad call cannot destroy live data.
    """
    data = parse_body(event)
    if data is None and (event.get('body') or '').strip():
        return err(400, 'invalid_json', 'Request body must be a JSON object')
    data = data or {}
    reset = bool(data.get('reset'))
    uid = user_id_of(claims) or SEED_ORGANIZER
    organizer = claims.get('name') or claims.get('email') or 'Seed'
    ts = now_iso()
    made, skipped = [], []
    try:
        for spec in SEED_EVENTS:
            event_id = spec['id']
            key = {'PK': f'EVENT#{event_id}', 'SK': f'EVENT#{event_id}'}
            existing = table.get_item(Key=key).get('Item')
            if existing and not reset:
                skipped.append(public_event(existing))
                continue
            if existing:
                conflicts = non_seed_registrations(event_id)
                if conflicts:
                    return err(409, 'conflict',
                               f'Refusing to reset {event_id}: {len(conflicts)} live user registration(s) exist')
                item = seed_event_item(spec, existing.get('organizerId', uid),
                                       existing.get('organizerName', organizer), ts)
                item['createdAt'] = existing.get('createdAt', ts)
            else:
                item = seed_event_item(spec, uid, organizer, ts)
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(PK)')
            write_seed_registrations(spec, ts)
            recount_registrations(event_id, ts)
            made.append(public_event(table.get_item(Key=key).get('Item', item)))
    except ClientError as e:
        return err(500, 'dynamodb_error', str(e))
    return created(
        {'created': made, 'skipped': skipped, 'reset': reset, 'total': len(SEED_EVENTS)},
        f'Seeded {len(made)} event(s), skipped {len(skipped)} already present',
    )


# ---------- router ----------

def handler(event, context=None):
    method = (event.get('requestContext', {}).get('http', {}).get('method') or event.get('httpMethod') or 'GET').upper()
    path = event.get('rawPath') or event.get('path') or '/'
    if method == 'OPTIONS':
        return preflight()
    claims = get_claims(event)
    authed = bool(user_id_of(claims))
    org = is_organizer(claims)
    parts = [p for p in path.split('/') if p]
    try:
        if method == 'GET' and path in ('/health', '/api/health'):
            return ok({'service': 'cloudcampus', 'time': now_iso()})
        if method == 'GET' and parts == ['events']:
            return list_events(event)
        if method == 'GET' and len(parts) == 2 and parts[0] == 'events':
            return get_event(event, parts[1])
        if method == 'POST' and parts == ['events']:
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            if not org:
                return err(403, 'forbidden', 'Organizer role required')
            return create_event(event, claims)
        if method == 'PATCH' and len(parts) == 2 and parts[0] == 'events':
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            if not org:
                return err(403, 'forbidden', 'Organizer role required')
            return update_event(event, claims, parts[1])
        if method == 'POST' and len(parts) == 3 and parts[0] == 'events' and parts[2] == 'cancel':
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            if not org:
                return err(403, 'forbidden', 'Organizer role required')
            return cancel_event(event, claims, parts[1])
        if method == 'POST' and len(parts) == 3 and parts[0] == 'events' and parts[2] == 'register':
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            return register_for_event(event, claims, parts[1])
        if method == 'POST' and len(parts) == 3 and parts[0] == 'events' and parts[2] == 'unregister':
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            return unregister_from_event(event, claims, parts[1])
        if method == 'GET' and parts == ['me', 'registrations']:
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            return my_registrations(event, claims)
        if method == 'GET' and parts == ['me', 'events']:
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            return my_events(event, claims)
        if method == 'GET' and len(parts) == 3 and parts[0] == 'events' and parts[2] == 'attendees':
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            if not org:
                return err(403, 'forbidden', 'Organizer role required')
            return event_attendees(event, claims, parts[1])
        if method == 'POST' and parts == ['seed']:
            if not authed:
                return err(401, 'unauthorized', 'Login required')
            if not org:
                return err(403, 'forbidden', 'Organizer role required')
            return seed_events(event, claims)
        return err(404, 'not_found', f'No route {method} {path}')
    except Exception as e:
        return err(500, 'server_error', str(e))

