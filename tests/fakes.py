"""A tiny in-memory stand-in for DynamoDB, for tests only.

It implements just the operations and expression forms that ``backend/app.py``
uses, but it *does* enforce the two DynamoDB rules the original registration
code broke:

* one ``TransactWriteItems`` call may not contain two operations on the same item
* every ``ExpressionAttributeValues`` / ``ExpressionAttributeNames`` entry must
  actually be referenced by the expression

Without those checks the old four-operation transaction would have "passed" a
naive mock, which is exactly why that bug shipped.
"""
import copy
import re
import threading

from botocore.exceptions import ClientError

GSI_KEYS = {
    'GSI1': ('GSI1PK', 'GSI1SK'),
    'GSI2': ('GSI2PK', 'GSI2SK'),
    'GSI3': ('GSI3PK', 'GSI3SK'),
}

# Real DynamoDB serialises writes to a single item, so the fake does too. This
# is what lets the parallel-registration test prove capacity is never exceeded.
_LOCK = threading.RLock()


def _err(code, message='', op='Unknown'):
    """Raise the same botocore error type the application catches."""
    return ClientError({'Error': {'Code': code, 'Message': message}}, op)

_PY_KEEP = {'and', 'or', 'not', 'in', 'True', 'False', 'None',
            'SET', 'REMOVE', 'ADD', 'DELETE',
            'item', 'names', 'values', 'attribute_not_exists'}


class DynamoError(Exception):
    """Unused placeholder kept out of the public surface; all raised errors are ClientError."""


class _Missing:
    """Stand-in for an attribute that is absent from the stored item."""

    def __eq__(self, other):
        return isinstance(other, _Missing)

    def __ne__(self, other):
        return not isinstance(other, _Missing)

    def __lt__(self, other):
        return False

    def __le__(self, other):
        return False

    def __gt__(self, other):
        return False

    def __ge__(self, other):
        return False

    def __repr__(self):
        return '<missing>'


_MISSING = _Missing()


def key_condition_parts(cond):
    """(attribute_name, [values]) from a boto3 Key condition object."""
    values = list(getattr(cond, '_values', ()))
    if not values:
        return None, []
    return getattr(values[0], 'name', None), values[1:]


def _lookup(token, mapping):
    """Resolve '#name' / ':value' against the supplied map, with or without prefix."""
    if not mapping:
        return token
    if token in mapping:
        return mapping[token]
    stripped = token[1:]
    if stripped in mapping:
        return mapping[stripped]
    return token


def _check_referenced(exprs, names, values):
    """DynamoDB rejects expression attribute entries no expression references."""
    joined = ' '.join(e for e in exprs if e)
    for k in (names or {}):
        if k not in joined:
            raise _err('ValidationException', f'ExpressionAttributeNames contains unused key: {k}')
    for k in (values or {}):
        token = k if k.startswith(':') else ':' + k
        if token not in joined:
            raise _err('ValidationException', f'ExpressionAttributeValues contains unused key: {token}')


def _python(expr, names, values):
    """Rewrite a DynamoDB expression into evaluable Python."""
    out = re.sub(r'^\s*SET\s+', '', expr or '', flags=re.I)
    out = re.sub(r'attribute_not_exists\(\s*([#\w]+)\s*\)',
                 lambda m: f'attribute_not_exists({m.group(1)!r})', out)
    # '#name' always denotes an item attribute; ':value' denotes a literal.
    out = re.sub(r'#(\w+)', lambda m: f'attr({_lookup("#" + m.group(1), names)!r})', out)
    out = re.sub(r':(\w+)', lambda m: repr(_lookup(':' + m.group(1), values)), out)
    out = out.replace('<>', '!=')
    out = re.sub(r'(?<![<>=!])=(?!=)', '==', out)
    for kw in ('AND', 'OR', 'NOT'):
        out = re.sub(rf'\b{kw}\b', kw.lower(), out)
    # any remaining bare identifier is an item attribute. The lookbehind also
    # excludes '-' so hyphens inside values (UUIDs, DATE#...#id) are never
    # mistaken for attribute names.
    return re.sub(r"(?<![\w.'\"\[\]-])([A-Za-z_]\w*)(?![\w'\"])(?!\s*\()",
                  lambda m: m.group(0) if m.group(0) in _PY_KEEP else f'attr({m.group(0)!r})', out)


def _eval_condition(expr, item, names, values, extra_exprs=()):
    _check_referenced([expr, *extra_exprs], names, values)
    return eval(_python(expr, names, values), {'__builtins__': {}}, _scope(item))  # noqa: S307


def _scope(item):
    return {
        'item': item or {}, 'names': {}, 'values': {},
        'attr': lambda k: (item or {}).get(k, _MISSING),
        'attribute_not_exists': lambda k: k not in (item or {}),
    }


def _apply_update_expression(expr, item, names, values, extra_exprs=()):
    _check_referenced([expr, *extra_exprs], names, values)
    body = re.sub(r'^\s*SET\s+', '', expr, flags=re.I)
    scope = _scope(item)
    for assignment in body.split(','):
        assignment = assignment.strip()
        if not assignment:
            continue
        lhs, rhs = (p.strip() for p in assignment.split('=', 1))
        item[_lookup(lhs, names)] = eval(_python(rhs, names, values), {'__builtins__': {}}, scope)  # noqa: S307
    return item


def _av(v):
    if isinstance(v, bool):
        return {'BOOL': v}
    if isinstance(v, (int, float)):
        return {'N': str(v)}
    return {'S': str(v)}


_TYPED = ('S', 'N', 'B', 'BOOL', 'SS', 'L', 'NULL', 'M', 'NS')


def _unav(v):
    """Accept both the low-level typed form ({'S': x}) and the plain resource form."""
    if isinstance(v, dict) and len(v) == 1:
        t, raw = next(iter(v.items()))
        if t in _TYPED:
            if t == 'N':
                return int(raw) if float(raw).is_integer() else float(raw)
            return raw
    return v


def _plain(values):
    return {k: _unav(v) for k, v in (values or {}).items()}


def _plain_item(item):
    return {k: _unav(v) for k, v in item.items()}


class FakeTable:
    """Resource-level API (get_item / put_item / update_item / query)."""

    def __init__(self, name='cloudcampus-events', page_size=None):
        self.name = name
        self.items = {}
        self.page_size = page_size
        self.calls = []
        self.client = FakeClient(self)

    @property
    def meta(self):
        return self

    @staticmethod
    def _key(k):
        # boto3 Table API passes plain strings; the low-level client API passes
        # {'S': ...}. Accept both so the fake can back either surface.
        def val(x):
            return x['S'] if isinstance(x, dict) else x
        return (val(k['PK']), val(k['SK']))

    def get_item(self, Key=None, **kw):
        self.calls.append('GetItem')
        item = self.items.get(self._key(Key))
        return {'Item': copy.deepcopy(item)} if item else {}

    def put_item(self, Item=None, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None, **kw):
        self.calls.append('PutItem')
        key = (Item['PK'], Item['SK'])
        with _LOCK:
            existing = self.items.get(key)
            if ConditionExpression and not _eval_condition(
                    ConditionExpression, existing, ExpressionAttributeNames,
                    _plain(ExpressionAttributeValues)):
                raise _err('ConditionalCheckFailedException', ConditionExpression, 'PutItem')
            if existing:
                return {}
            self.items[key] = copy.deepcopy(Item)
        return {'Attributes': copy.deepcopy(Item)}

    def update_item(self, Key=None, UpdateExpression=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ReturnValues=None, **kw):
        self.calls.append('UpdateItem')
        key = self._key(Key)
        values = _plain(ExpressionAttributeValues)
        with _LOCK:
            existing = self.items.get(key)
            if ConditionExpression and not _eval_condition(
                    ConditionExpression, existing, ExpressionAttributeNames, values,
                    extra_exprs=(UpdateExpression,)):
                raise _err('ConditionalCheckFailedException', ConditionExpression, 'UpdateItem')
            if existing is None:
                raise _err('ConditionalCheckFailedException', 'The conditional request failed', 'UpdateItem')
            _apply_update_expression(UpdateExpression, existing, ExpressionAttributeNames, values,
                                     extra_exprs=(ConditionExpression,))
            snapshot = copy.deepcopy(existing)
        if ReturnValues == 'ALL_NEW':
            return {'Attributes': snapshot}
        return {}

    def query(self, IndexName=None, KeyConditionExpression=None, ExclusiveStartKey=None, **kw):
        self.calls.append('Query')
        if IndexName:
            pk_attr, sk_attr = GSI_KEYS[IndexName]
            sort_key = lambda i: (i.get(pk_attr, ''), i.get(sk_attr, ''))  # noqa: E731
        else:
            sort_key = lambda i: (i.get('PK', ''), i.get('SK', ''))  # noqa: E731
        want_attr, want_vals = key_condition_parts(KeyConditionExpression)
        pool = [i for i in self.items.values() if i.get(want_attr) in want_vals]
        pool.sort(key=sort_key)
        start = 0
        if ExclusiveStartKey:
            want = (ExclusiveStartKey.get('PK'), ExclusiveStartKey.get('SK'))
            start = next((n for n, i in enumerate(pool) if (i['PK'], i['SK']) == want), len(pool))
        page = pool[start:start + self.page_size] if self.page_size else pool[start:]
        out = {'Items': copy.deepcopy(page), 'Count': len(page), 'ScannedCount': len(page)}
        nxt = start + len(page)
        if nxt < len(pool):
            out['LastEvaluatedKey'] = {'PK': pool[nxt]['PK'], 'SK': pool[nxt]['SK']}
        return out


class FakeClient:
    """Low-level client API - only transact_write_items is exercised."""

    def __init__(self, table):
        self._table = table

    def transact_write_items(self, TransactItems=None, **kw):
        self._table.calls.append('TransactWriteItems')
        seen = set()
        for op in TransactItems:
            name, body = next(iter(op.items()))
            if name in ('Put',):
                key = (body['Item']['PK']['S'], body['Item']['SK']['S'])
            elif name in ('Update', 'ConditionCheck', 'Delete'):
                key = (body['Key']['PK']['S'], body['Key']['SK']['S'])
            else:
                raise _err('ValidationException', f'unsupported transaction op: {name}')
            if key in seen:
                raise _err('ValidationException', 'Transaction request cannot include multiple operations on one item')
            seen.add(key)

        staged = copy.deepcopy(self._table.items)
        with _LOCK:
            for op in TransactItems:
                name, body = next(iter(op.items()))
                names = body.get('ExpressionAttributeNames')
                values = _plain(body.get('ExpressionAttributeValues'))
                if name == 'Put':
                    for attr in ('ConditionExpression', 'UpdateExpression'):
                        if body.get(attr):
                            _check_referenced([body[attr]], names, values)
                    key = (body['Item']['PK']['S'], body['Item']['SK']['S'])
                    if body.get('ConditionExpression') and not _eval_condition(
                            body['ConditionExpression'], staged.get(key), names, values):
                        raise _err('TransactionCanceledException', '', 'TransactWriteItems')
                    staged[key] = _plain_item(body['Item'])
                else:
                    _check_referenced([body.get('ConditionExpression'), body.get('UpdateExpression')],
                                      names, values)
                    key = (body['Key']['PK']['S'], body['Key']['SK']['S'])
                    existing = staged.get(key)
                    if body.get('ConditionExpression') and not _eval_condition(
                            body['ConditionExpression'], existing, names, values,
                            extra_exprs=(body.get('UpdateExpression'),)):
                        raise _err('TransactionCanceledException', '', 'TransactWriteItems')
                    if existing is None:
                        raise _err('TransactionCanceledException', '', 'TransactWriteItems')
                    if name == 'Update':
                        _apply_update_expression(body['UpdateExpression'], existing, names, values,
                                                 extra_exprs=(body.get('ConditionExpression'),))
                    staged[key] = existing
            self._table.items = staged
        return {}


def install(app, page_size=None, table_name='cloudcampus-events'):
    """Point ``app.table`` at a fresh fake table and return it.

    Also patches ``app.boto3.client`` so that production code calling
    ``boto3.client('dynamodb')`` (instead of ``table.meta.client``) gets the
    fake client, keeping the unit tests hermetic with no AWS calls.
    Patching is scoped to ``app.boto3.client`` so it does not leak between
    tests.
    """
    table = FakeTable(table_name, page_size=page_size)
    app.table = table
    app._orig_boto3_client = app.boto3.client

    def _patched_client(service_name, *args, **kwargs):
        if service_name == 'dynamodb':
            return table.client
        return app._orig_boto3_client(service_name, *args, **kwargs)

    app.boto3.client = _patched_client
    return table


def uninstall(app):
    """Restore the original ``app.boto3.client`` after tests."""
    if hasattr(app, '_orig_boto3_client'):
        app.boto3.client = app._orig_boto3_client

