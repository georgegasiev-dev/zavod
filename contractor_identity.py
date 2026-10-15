"""Counterparty identities shared by historical cleanup and every import."""
import copy
import hashlib
import re
from collections import defaultdict


def norm(value):
    value = (value or '').translate(str.maketrans({
        '«': '"', '»': '"', '“': '"', '”': '"', '„': '"',
        '–': '-', '—': '-', '‑': '-'}))
    return ' '.join(value.casefold().split())


def clean_label(value):
    value = re.sub(r'[,\s]*(?:ИНН|INN)\s*[:：]?\s*\d*\s*$', '', value or '', flags=re.I)
    return ' '.join(value.strip(' ,').split())


def identity_key(value):
    value = norm(clean_label(value)).replace('ё', 'е')
    if '//' in value:
        fields = value.split('//')
        value = fields[1] if fields[0].startswith('пао сбербанк') and len(fields) > 1 else fields[0]
    forms = [('общество с ограниченной ответственностью', 'ооо'),
             ('закрытое акционерное общество', 'зао'),
             ('открытое акционерное общество', 'оао'),
             ('публичное акционерное общество', 'пао'),
             ('акционерное общество', 'ао'),
             ('индивидуальный предприниматель', 'ип')]
    for full, short in forms:
        if value.startswith(full + ' '):
            value = short + value[len(full):]
            break
    value = re.sub(r'["«»“”„.,()]', ' ', value)
    value = re.sub(r'\s*-\s*', '-', value)
    value = ' '.join(value.split())
    person = value[3:] if value.startswith('ип ') else value
    if re.fullmatch(r'[а-я-]{2,} [а-я-]{2,} [а-я-]{2,}(?:вич|вна|ич|оглы|кызы)', person):
        value = 'ип ' + person
    return value


def name_id(key):
    return 'name:' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def valid_inn(value):
    value = str(value or '')
    if not value.isascii() or not value.isdigit() or len(set(value)) == 1:
        return False
    def digit(weights):
        return str(sum(w * int(c) for w, c in zip(weights, value)) % 11 % 10)
    if len(value) == 10:
        return digit([2, 4, 10, 3, 5, 9, 4, 6, 8]) == value[-1]
    if len(value) == 12:
        return (digit([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == value[-2]
                and digit([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == value[-1])
    return False


def extract_inn(name):
    raw = (name or '').strip()
    values = set(re.findall(r'\b(?:ИНН|INN)\s*[:：]?\s*(\d{10,12})(?!\d)', raw, re.I))
    if raw.isdigit():
        values.add(raw)
    return next(iter(values)) if len(values) == 1 and valid_inn(next(iter(values))) else None


def tax_fragment(name):
    match = re.search(r'\b(?:ИНН|INN)\s*[:：]?\s*(\d*)\s*$', name or '', re.I)
    return match.group(1) if match else None


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_entities (
        id TEXT PRIMARY KEY, inn TEXT NOT NULL UNIQUE, name TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_aliases (
        alias TEXT PRIMARY KEY, entity_id TEXT NOT NULL, evidence TEXT NOT NULL,
        FOREIGN KEY(entity_id) REFERENCES contractor_entities(id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_label_aliases (
        alias TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
        canonical TEXT NOT NULL, evidence TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS contractor_merge_log (
        migration TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        backup_path TEXT NOT NULL, backup_sha256 TEXT NOT NULL, report TEXT NOT NULL)''')


def registry(conn):
    result = {'inns': {}, 'keys': defaultdict(dict), 'labels': {}, 'exact': {}}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'contractor_entities' in tables:
        entities = {}
        for row in conn.execute('SELECT id, name, inn FROM contractor_entities'):
            item = tuple(row)
            entities[row[0]] = item
            result['inns'][row[2]] = item
            result['keys'][identity_key(row[1])][row[2]] = item
        for alias, entity_id in conn.execute('SELECT alias, entity_id FROM contractor_aliases'):
            item = entities.get(entity_id)
            if item:
                result['exact'][norm(alias)] = item
                result['keys'][identity_key(alias)][item[2]] = item
    if 'contractor_label_aliases' in tables:
        for alias, key, canonical in conn.execute('SELECT alias, identity_key, canonical FROM contractor_label_aliases'):
            item = (name_id(identity_key(canonical)), canonical, None)
            result['labels'][key] = item
            result['exact'][norm(alias)] = item
    return result


def resolve(conn, name, inn=None, register=False, _registry=None):
    explicit = str(inn or '') or extract_inn(name)
    embedded = extract_inn(name)
    if explicit and not valid_inn(explicit):
        raise ValueError('Invalid counterparty INN')
    if embedded and explicit and embedded != explicit:
        raise ValueError('Conflicting counterparty INNs')
    reg = _registry if _registry is not None else registry(conn)
    key = identity_key(name)
    if explicit:
        if register:
            ensure_schema(conn)
            display = clean_label(name) + ', ИНН: ' + explicit
            conn.execute('INSERT OR IGNORE INTO contractor_entities VALUES (?, ?, ?)',
                         ('inn:' + explicit, explicit, display))
            if embedded:
                prior = conn.execute('SELECT entity_id FROM contractor_aliases WHERE alias=?', (norm(name),)).fetchone()
                if prior and prior[0] != 'inn:' + explicit:
                    raise ValueError('Conflicting explicit INN alias')
                conn.execute('INSERT OR IGNORE INTO contractor_aliases VALUES (?, ?, ?)',
                             (norm(name), 'inn:' + explicit, 'explicit_valid_inn'))
            item = tuple(conn.execute('SELECT id, name, inn FROM contractor_entities WHERE inn=?', (explicit,)).fetchone())
            reg['inns'][explicit] = item
            reg['keys'][key][explicit] = item
        return reg['inns'].get(explicit, ('inn:' + explicit, name, explicit))
    members = reg['keys'].get(key, {})
    # A stored bare alias must not override new evidence of a namesake.
    if len(members) > 1:
        return None, name, None
    fragment = tax_fragment(name)
    if len(members) == 1:
        item = next(iter(members.values()))
        if fragment and not item[2].startswith(fragment):
            return None, name, None
        return item
    exact = reg['exact'].get(norm(name))
    if exact:
        return exact
    if key in reg['labels']:
        return reg['labels'][key]
    if register and key:
        if fragment and len(fragment) in (10,12) and not valid_inn(fragment) and len(name) != 60:
            return None, name, None
        ensure_schema(conn)
        canonical = clean_label(name)
        item = (name_id(identity_key(canonical)), canonical, None)
        conn.execute('INSERT OR IGNORE INTO contractor_label_aliases VALUES (?, ?, ?, ?)',
                     (norm(name), key, canonical, 'normalized_full_name_without_inn'))
        reg['labels'][key] = item
        return item
    return None, name, None


def canonicalize_data(conn, data):
    ensure_schema(conn)
    reg = registry(conn)
    result = copy.deepcopy(data)
    # Register all explicit identifiers first, so a single batch does not
    # depend on row order when it contains a bare name and a tagged name.
    for key in ('ops', 'unknown'):
        for op in result.get(key, []):
            if op.get('contractor_inn') or extract_inn(op.get('contractor', '')):
                resolve(conn, op.get('contractor', ''), op.get('contractor_inn'), True, reg)
    for key in ('ops', 'unknown'):
        for op in result.get(key, []):
            name = op.get('contractor', '')
            entity_id, canonical, inn = resolve(conn, name, op.get('contractor_inn'), True, reg)
            if entity_id:
                op.setdefault('contractor_original', name)
                op.update(contractor=canonical, contractor_id=entity_id)
                if inn:
                    op['contractor_inn'] = inn
    return result


def canonical_name(conn, name):
    return resolve(conn, name)[1]


def name_resolver(conn):
    reg = registry(conn)
    return lambda name, inn=None: resolve(conn, name, inn, _registry=reg)[1]

