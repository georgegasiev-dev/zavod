"""Idempotent reviewed migration, with a verified full SQLite backup first.

Only the two identities requested by the owner are approved below. Similar
spellings (including Стандарт-Старуд) remain separate for human verification.
Run directly for an audit; --apply performs the reviewed migration.
"""
import argparse
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from contractor_identity import (norm, extract_inn, ensure_schema,
                                 canonicalize_data, valid_inn)

MIGRATION = '2026-10-15-reviewed-contractor-identities-v1'
REVIEWED = [
    {'name': 'ИП Айрапетян Арсен Камоевич', 'inn': '522802685180',
     'evidence': 'Owner identified full name and INN; matching full bank labels.'},
    {'name': 'ООО "Стандарт-Старвуд"', 'inn': '7820078617',
     'evidence': 'Owner reported duplicate Standard-Star entries; exact full name and a single valid INN in bank labels.'},
]
IDENTITY_FIELDS = {'contractor', 'contractor_id', 'contractor_inn', 'contractor_original'}


def without_inn(name):
    return norm(re.sub(r',?\s*ИНН\s*:\s*\d+\s*$', '', name or '', flags=re.I))


def payment_fingerprint(data):
    result = copy.deepcopy(data)
    for key in ('ops', 'unknown'):
        for op in result.get(key, []):
            for field in IDENTITY_FIELDS:
                op.pop(field, None)
    return json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)


def audit(conn):
    months = conn.execute('SELECT month, data FROM month_data ORDER BY month').fetchall()
    names = {}
    for month, payload in months:
        for op in json.loads(payload).get('ops', []):
            name = op.get('contractor', '')
            entry = names.setdefault(name, {'name': name, 'inn': extract_inn(name),
                                            'operations': 0, 'months': set()})
            entry['operations'] += 1
            entry['months'].add(month)
    groups = {}
    for entry in names.values():
        groups.setdefault(without_inn(entry['name']), []).append(entry)
    return {'months': len(months), 'operations': sum(n['operations'] for n in names.values()),
            'name_variants': len(names), 'candidates': [
                [{**e, 'months': sorted(e['months'])} for e in entries]
                for entries in groups.values() if len(entries) > 1]}


def apply_reviewed(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        return {'status': 'no_database'}
    conn = sqlite3.connect(path, timeout=60)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='month_data'").fetchone():
            conn.rollback()
            return {'status': 'no_month_data'}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='contractor_merge_log'").fetchone():
            row = conn.execute('SELECT report FROM contractor_merge_log WHERE migration=?',
                               (MIGRATION,)).fetchone()
            if row:
                conn.rollback()
                return {'status': 'already_applied', 'report': json.loads(row[0])}
        rows = conn.execute('SELECT month, data FROM month_data ORDER BY month').fetchall()
        labels = {op.get('contractor', '') for _, payload in rows
                  for op in json.loads(payload).get('ops', [])}
        approved = []
        for profile in REVIEWED:
            assert valid_inn(profile['inn'])
            aliases = [label for label in labels if without_inn(label) == norm(profile['name'])]
            inns = {extract_inn(label) for label in aliases} - {None}
            if inns - {profile['inn']}:
                raise ValueError('Conflicting INNs for reviewed counterparty: ' + profile['name'])
            if profile['inn'] in inns:
                approved.append((profile, aliases))
        if not approved:
            conn.rollback()
            return {'status': 'no_verified_matches'}
        # A reserved write lock prevents concurrent writers between snapshot and commit.
        backup_dir = path.parent / 'backups'
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = backup_dir / ('before-contractor-merge-' + stamp + '-' + uuid4().hex + '.db')
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as dest:
            source.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        ensure_schema(conn)
        report = {'migration': MIGRATION, 'backup_path': str(backup),
                  'backup_sha256': backup_hash, 'groups': [], 'operations_updated': 0,
                  'months_updated': [], 'payment_fields_unchanged': True}
        for profile, aliases in approved:
            entity_id = 'inn:' + profile['inn']
            canonical = profile['name'] + ', ИНН: ' + profile['inn']
            conn.execute('INSERT INTO contractor_entities VALUES (?, ?, ?) '
                         'ON CONFLICT(inn) DO UPDATE SET name=excluded.name',
                         (entity_id, profile['inn'], canonical))
            for label in aliases + [profile['name'], canonical]:
                key = norm(label)
                old = conn.execute('SELECT entity_id FROM contractor_aliases WHERE alias=?', (key,)).fetchone()
                if old and old[0] != entity_id:
                    raise ValueError('Conflicting existing alias')
                conn.execute('INSERT OR IGNORE INTO contractor_aliases VALUES (?, ?, ?)',
                             (key, entity_id, profile['evidence']))
            # Keep original metadata rows; create one combined comment without loss.
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'contractor_details' in tables:
                details = [r for r in conn.execute('SELECT contractor, comment, added_at FROM contractor_details')
                           if norm(r[0]) in {norm(a) for a in aliases + [profile['name'], canonical]}]
                comments = list(dict.fromkeys(r[1] for r in details if r[1]))
                dates = [r[2] for r in details if r[2]]
                if details:
                    conn.execute('INSERT INTO contractor_details VALUES (?, ?, ?) '
                                 'ON CONFLICT(contractor) DO UPDATE SET comment=excluded.comment, added_at=excluded.added_at',
                                 (norm(canonical), '\n\n'.join(comments), min(dates) if dates else ''))
            if 'contractor_map' in tables:
                mappings = [r for r in conn.execute('SELECT contractor, cat FROM contractor_map')
                            if norm(r[0]) in {norm(a) for a in aliases + [profile['name'], canonical]}]
                categories = {r[1] for r in mappings}
                # Conflicting defaults stay on their original aliases. Never change payment categories.
                if len(categories) == 1:
                    conn.execute('INSERT OR IGNORE INTO contractor_map VALUES (?, ?, ?)',
                                 (norm(canonical), next(iter(categories)), stamp))
                report.setdefault('category_conflicts', []).extend(
                    [{'name': profile['name'], 'defaults': sorted(categories)}] if len(categories) > 1 else [])
            report['groups'].append({'name': canonical, 'aliases': aliases, 'evidence': profile['evidence']})
        for month, payload in rows:
            old = json.loads(payload)
            # Only approved aliases are changed in this migration. Other identities
            # are handled on subsequent imports, never inferred from similar names.
            new = copy.deepcopy(old)
            changed = 0
            for key in ('ops', 'unknown'):
                for op in new.get(key, []):
                    alias = conn.execute('SELECT 1 FROM contractor_aliases WHERE alias=?',
                                         (norm(op.get('contractor', '')),)).fetchone()
                    if alias:
                        patched = canonicalize_data(conn, {'ops': [op]})['ops'][0]
                        if patched != op:
                            changed += key == 'ops'
                        op.update(patched)
            if payment_fingerprint(old) != payment_fingerprint(new):
                raise RuntimeError('Payment invariant failed')
            if new != old:
                conn.execute('UPDATE month_data SET data=? WHERE month=?',
                             (json.dumps(new, ensure_ascii=False, allow_nan=False), month))
                report['months_updated'].append(month)
                report['operations_updated'] += changed
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key check failed')
        conn.execute('INSERT INTO contractor_merge_log VALUES (?, ?, ?, ?, ?)',
                     (MIGRATION, stamp, str(backup), backup_hash, json.dumps(report, ensure_ascii=False)))
        conn.commit()
        return {'status': 'applied', 'report': report}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.getenv('DB_PATH', 'data/novator.db'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        result = apply_reviewed(args.db)
    else:
        with sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            result = audit(conn)
    print(json.dumps(result, ensure_ascii=False, indent=2))
