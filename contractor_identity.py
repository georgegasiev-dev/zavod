"""Counterparty identity: verified INNs and explicitly reviewed name aliases.

Names alone never establish a new link to an INN. Original bank labels remain
on each operation, and payment categories are independent of legal identity.
"""
import copy
import re
import sqlite3


def norm(value):
    value = (value or '').translate(str.maketrans({'«': '"', '»': '"',
        '“': '"', '”': '"', '„': '"', '–': '-', '—': '-', '‑': '-'}))
    return ' '.join(value.casefold().split())


def valid_inn(value):
    value = str(value or '')
    if not value.isascii() or not value.isdigit() or len(set(value)) == 1:
        return False
    def digit(weights, chars):
        return str(sum(w * int(c) for w, c in zip(weights, chars)) % 11 % 10)
    if len(value) == 10:
        return digit([2, 4, 10, 3, 5, 9, 4, 6, 8], value) == value[-1]
    if len(value) == 12:
        return (digit([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], value) == value[-2]
                and digit([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], value) == value[-1])
    return False


def extract_inn(name):
    raw = (name or '').strip()
    values = re.findall(r'\bИНН\s*[:：]?\s*(\d{10,12})(?!\d)', raw, re.I)
    if raw.isdigit():
        values.append(raw)
    values = set(values)
    if len(values) == 1 and valid_inn(next(iter(values))):
        return next(iter(values))
    return None


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_entities (
        id TEXT PRIMARY KEY, inn TEXT NOT NULL UNIQUE, name TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_aliases (
        alias TEXT PRIMARY KEY, entity_id TEXT NOT NULL,
        evidence TEXT NOT NULL,
        FOREIGN KEY(entity_id) REFERENCES contractor_entities(id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_merge_log (
        migration TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        backup_path TEXT NOT NULL, backup_sha256 TEXT NOT NULL,
        report TEXT NOT NULL)''')


def resolve(conn, name, inn=None, register=False):
    """Return (entity id, canonical label, INN); fail closed on conflicting INNs."""
    explicit = str(inn or '') or extract_inn(name)
    if explicit and not valid_inn(explicit):
        raise ValueError('Invalid counterparty INN')
    embedded = extract_inn(name)
    if embedded and explicit and embedded != explicit:
        raise ValueError('Conflicting counterparty INNs')
    try:
        alias = conn.execute('''SELECT e.id, e.name, e.inn FROM contractor_aliases a
            JOIN contractor_entities e ON e.id=a.entity_id WHERE a.alias=?''',
                             (norm(name),)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        alias = None
    if alias:
        if explicit and explicit != alias[2]:
            raise ValueError('Alias is already linked to a different INN')
        return tuple(alias)
    if not explicit:
        return None, name, None
    if register:
        ensure_schema(conn)
        entity_id = 'inn:' + explicit
        display_name = name if embedded else name + ', ИНН: ' + explicit
        conn.execute('INSERT OR IGNORE INTO contractor_entities VALUES (?, ?, ?)',
                     (entity_id, explicit, display_name))
        # A bank may supply the same legal name for two distinct INNs in a
        # separate column. Never learn an unqualified name alias from that.
        if embedded:
            conn.execute('INSERT INTO contractor_aliases VALUES (?, ?, ?)',
                         (norm(name), entity_id, 'explicit_valid_inn'))
    try:
        row = conn.execute('SELECT id, name, inn FROM contractor_entities WHERE inn=?',
                           (explicit,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        row = None
    return tuple(row) if row else ('inn:' + explicit, name, explicit)


def canonicalize_data(conn, data):
    result = copy.deepcopy(data)
    for key in ('ops', 'unknown'):
        for op in result.get(key, []):
            name = op.get('contractor', '')
            entity_id, canonical, inn = resolve(conn, name, op.get('contractor_inn'), True)
            if entity_id:
                op.setdefault('contractor_original', name)
                op.update(contractor=canonical, contractor_id=entity_id, contractor_inn=inn)
    return result


def canonical_name(conn, name):
    return resolve(conn, name)[1]


def name_resolver(conn):
    """Read a consistent registry snapshot once for a whole API response/import."""
    try:
        aliases = dict(conn.execute('''SELECT a.alias, e.name FROM contractor_aliases a
            JOIN contractor_entities e ON e.id=a.entity_id'''))
        inns = dict(conn.execute('SELECT inn, name FROM contractor_entities'))
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        aliases, inns = {}, {}
    return lambda name: aliases.get(norm(name), inns.get(extract_inn(name), name))
