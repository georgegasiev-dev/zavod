"""Whole-directory consolidation: exact legal names, INNs and legacy formatting."""
import copy
from contextlib import closing
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from contractor_identity import (identity_key, clean_label, norm, extract_inn,
    valid_inn, tax_fragment, ensure_schema, canonicalize_data, registry, name_resolver)
from migrate_contractors import payment_fingerprint

MIGRATION = '2026-10-15-all-contractor-identities-v2'


def build_plan(records):
    """No fuzzy matching: different INNs always prevent an inferred name link."""
    info = {}
    for record in records:
        name = record.get('contractor', '')
        if not name:
            continue
        entry = info.setdefault(name, {'name': name, 'inns': set(), 'operations': 0})
        entry['operations'] += int(record.get('_operation', False))
        inn = str(record.get('contractor_inn') or '') or extract_inn(name)
        if inn and valid_inn(inn):
            entry['inns'].add(inn)
    parent = {n: n for n in info}
    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n
    def inns(n):
        return set().union(*(i['inns'] for k,i in info.items() if find(k) == find(n)))
    def union(names):
        names = list(names)
        if not names:
            return False
        combined = set().union(*(inns(n) for n in names))
        if len(combined) > 1:
            return False
        first = find(names[0])
        for name in names[1:]:
            parent[find(name)] = first
        return True
    tax_groups, key_groups = defaultdict(list), defaultdict(list)
    for name, entry in info.items():
        if len(entry['inns']) == 1:
            tax_groups[next(iter(entry['inns']))].append(name)
        key_groups[identity_key(name)].append(name)
    for names in tax_groups.values():
        union(names)
    conflicts = []
    for key, names in key_groups.items():
        known = set().union(*(info[n]['inns'] for n in names))
        if len(known) > 1:
            conflicts.append({'key': key, 'reason': 'different_inns', 'inns': sorted(known)})
            continue
        target = next(iter(known)) if known else None
        compatible = []
        for name in names:
            fragment = tax_fragment(name)
            if target and fragment and not target.startswith(fragment):
                conflicts.append({'name': name, 'reason': 'incompatible_inn_fragment'})
                continue
            # A full invalid INN is not evidence for merging with a valid one.
            if fragment and len(fragment) in (10,12) and not valid_inn(fragment) and len(name) != 60:
                conflicts.append({'name': name, 'reason': 'invalid_full_inn'})
                continue
            compatible.append(name)
        union(compatible)
    # Full registered name cut by the old 60-character importer; only a unique
    # continuation is accepted. Similar company suffixes never use this rule.
    for key, names in key_groups.items():
        if len(key) < 25 or not any(len(n.strip()) == 60 for n in names):
            continue
        possible = [other for other in key_groups if other != key and other.startswith(key)]
        roots = {find(key_groups[k][0]) for k in possible}
        if len(roots) == 1:
            union(names + [key_groups[possible[0]][0]])
    # The owner identified the Standard-Star variants in the original request.
    # This reviewed missing-letter spelling has one possible full legal name.
    wrong = key_groups.get('ооо стандарт-старуд', [])
    right = key_groups.get('ооо стандарт-старвуд', [])
    if wrong and right and set().union(*(inns(n) for n in right)) == {'7820078617'}:
        union(wrong + right)
    groups = defaultdict(list)
    for name in info:
        groups[find(name)].append(name)
    plan = []
    for aliases in groups.values():
        known = set().union(*(info[n]['inns'] for n in aliases))
        if len(known) > 1:
            conflicts.append({'names': sorted(aliases), 'reason': 'conflicting_record_inns'})
            continue
        inn = next(iter(known)) if known else None
        def score(name):
            label = clean_label(name)
            return ('//' in label, label.isdigit(), len(label), -info[name]['operations'], label)
        canonical = clean_label(min(aliases, key=score))
        if inn:
            canonical += ', ИНН: ' + inn
        plan.append({'inn': inn, 'name': canonical, 'aliases': sorted(aliases),
                     'operations': sum(info[n]['operations'] for n in aliases),
                     'evidence': 'same_valid_inn_or_unique_normalized_full_legal_name'})
    return {'groups': sorted(plan, key=lambda g:g['name']), 'conflicts': conflicts}


def load_records(rows, conn=None):
    records = []
    for month, payload in rows:
        for op in json.loads(payload).get('ops', []):
            records.append({**op, '_operation': True})
            if op.get('contractor_original'):
                records.append({'contractor': op['contractor_original'],
                                'contractor_inn': op.get('contractor_inn')})
    if conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('contractor_map', 'contractor_details'):
            if table in tables:
                records.extend({'contractor': r[0]} for r in conn.execute('SELECT contractor FROM ' + table))
    return records


def visible_names(rows, resolver=lambda name:name):
    return {norm(resolver(op.get('contractor',''))) for _, payload in rows
            for op in json.loads(payload).get('ops',[]) if op.get('is_debit') and op.get('contractor')}


def apply_all(db_path):
    path = Path(db_path).resolve()
    if not path.is_file():
        return {'status': 'no_database'}
    conn = sqlite3.connect(path, timeout=60)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'month_data' not in tables:
            conn.rollback()
            return {'status': 'no_month_data'}
        if 'contractor_merge_log' in tables:
            done = conn.execute('SELECT report FROM contractor_merge_log WHERE migration=?', (MIGRATION,)).fetchone()
            if done:
                conn.rollback()
                return {'status':'already_applied', 'report':json.loads(done[0])}
        rows = conn.execute('SELECT month,data FROM month_data ORDER BY month').fetchall()
        plan = build_plan(load_records(rows, conn))
        stamp = datetime.now(timezone.utc).isoformat()
        backup_dir = path.parent / 'backups'
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup = backup_dir / ('before-all-contractors-' + uuid4().hex + '.db')
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(backup)) as dest:
            src.backup(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        digest = hashlib.sha256(backup.read_bytes()).hexdigest()
        ensure_schema(conn)
        old_registry = registry(conn)
        debit_names = {op.get('contractor','') for _,p in rows for op in json.loads(p).get('ops',[]) if op.get('is_debit')}
        report = {'migration':MIGRATION, 'backup_path':str(backup), 'backup_sha256':digest,
                  'payment_fields_unchanged':True, 'operations_updated':0,
                  'operations_verified':sum(len(json.loads(p).get('ops',[])) for _,p in rows),
                  'months_verified':len(rows), 'cards_before':len(visible_names(rows)),
                  'groups_merged':0, 'groups':[], 'conflicts':plan['conflicts']}
        for group in plan['groups']:
            aliases = group['aliases']
            inn = group['inn']
            canonical = old_registry['inns'].get(inn, (None,group['name'],None))[1] if inn else group['name']
            group['name'] = canonical
            if inn:
                entity_id = 'inn:' + inn
                conn.execute('INSERT INTO contractor_entities VALUES (?, ?, ?) ON CONFLICT(inn) DO NOTHING',
                             (entity_id,inn,canonical))
                for alias in aliases + [canonical]:
                    prior = conn.execute('SELECT entity_id FROM contractor_aliases WHERE alias=?', (norm(alias),)).fetchone()
                    if prior and prior[0] != entity_id:
                        raise ValueError('Conflicting alias: ' + alias)
                    conn.execute('INSERT OR IGNORE INTO contractor_aliases VALUES (?, ?, ?)',
                                 (norm(alias),entity_id,group['evidence']))
            else:
                for alias in aliases + [canonical]:
                    conn.execute('INSERT OR REPLACE INTO contractor_label_aliases VALUES (?, ?, ?, ?)',
                                 (norm(alias),identity_key(alias),canonical,group['evidence']))
            alias_keys = {norm(a) for a in aliases + [canonical]}
            if 'contractor_details' in tables:
                details = [r for r in conn.execute('SELECT contractor,comment,added_at FROM contractor_details')
                           if norm(r[0]) in alias_keys]
                if details:
                    comments = list(dict.fromkeys(r[1] for r in details if r[1]))
                    dates = [r[2] for r in details if r[2]]
                    conn.execute('INSERT INTO contractor_details VALUES (?, ?, ?) ON CONFLICT(contractor) '
                                 'DO UPDATE SET comment=excluded.comment,added_at=excluded.added_at',
                                 (norm(canonical),'\n\n'.join(comments),min(dates) if dates else ''))
            if 'contractor_map' in tables:
                cats = {r[1] for r in conn.execute('SELECT contractor,cat FROM contractor_map') if norm(r[0]) in alias_keys}
                if len(cats) == 1:
                    conn.execute('INSERT OR IGNORE INTO contractor_map VALUES (?, ?, ?)',
                                 (norm(canonical),next(iter(cats)),stamp))
            group['visible_variants'] = len({norm(a) for a in aliases if a in debit_names})
            if group['visible_variants'] > 1:
                report['groups_merged'] += 1
            report['groups'].append(group)
        after_rows = []
        for month,payload in rows:
            before = json.loads(payload)
            after = canonicalize_data(conn,before)
            if payment_fingerprint(before) != payment_fingerprint(after):
                raise RuntimeError('Payment fields changed in ' + month)
            report['operations_updated'] += sum(a != b for a,b in zip(before.get('ops',[]),after.get('ops',[])))
            encoded = json.dumps(after,ensure_ascii=False,allow_nan=False)
            after_rows.append((month,encoded))
            if after != before:
                conn.execute('UPDATE month_data SET data=? WHERE month=?',(encoded,month))
        report['cards_after'] = len(visible_names(after_rows,name_resolver(conn)))
        # A verified key may have one canonical name; residual keys with different
        # INNs are conflicts, rather than candidates silently merged by name.
        residual = defaultdict(set)
        residual_inns = defaultdict(set)
        for _,p in after_rows:
            for op in json.loads(p).get('ops',[]):
                key = identity_key(op.get('contractor',''))
                residual[key].add(norm(op.get('contractor','')))
                inn = op.get('contractor_inn') or extract_inn(op.get('contractor',''))
                if inn:
                    residual_inns[key].add(inn)
        report['remaining_duplicate_keys'] = sum(len(names)>1 and len(residual_inns[k])<=1
                                                  for k,names in residual.items())
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Foreign key check failed')
        conn.execute('INSERT INTO contractor_merge_log VALUES (?, ?, ?, ?, ?)',
                     (MIGRATION,stamp,str(backup),digest,json.dumps(report,ensure_ascii=False)))
        conn.commit()
        return {'status':'applied','report':report}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

