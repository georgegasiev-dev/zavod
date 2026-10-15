# Counterparty identities

The first application start after this change runs the idempotent reviewed
migration before requests and bank-import schedules start. Only exact full-name
variants of the two reviewed identities in `migrate_contractors.REVIEWED` are
eligible. A matching valid INN must already occur in the stored bank labels.
Whitespace, case and typographic quotes are cosmetic; spelling differences,
initials and similar names are never sufficient. Conflicting INNs abort the
transaction. Other audit candidates require independent verification.

Before any merge, SQLite's backup API creates a complete snapshot in
`<database directory>/backups/before-contractor-merge-<timestamp>-<uuid>.db`.
The file is created with owner-only permissions and checked with
`PRAGMA integrity_check`. Its SHA-256 and the alias evidence are recorded in
`contractor_merge_log`. Backups remain on the server; they include credentials
stored in SQLite and must never be committed or uploaded to public artifacts.

Each operation keeps its original bank label in `contractor_original` and gets
a stable `contractor_id` and `contractor_inn`. Only identity fields change.
The migration compares every other operation field, metadata, ordering, counts
and stored totals before commit. Comments are combined without deleting source
rows; conflicting default categories stay in their original mapping rows.
Individual payment categories and manual-category flags do not change.

New imports retain full bank labels and accept a separate counterparty INN
column. A UNIQUE constraint on INN and a primary key on normalized alias prevent
duplicate registered identities. An unqualified name is never automatically
learned from a separate INN column: identical names with different INNs stay
separate. Date-overlap replacement during import retains its existing behavior;
manual-category matching now uses the stable entity identity.

`python migrate_contractors.py --db /path/to/novator.db` is a read-only audit.
`--apply` explicitly runs the same migration used at startup. Audit output
contains private counterparty labels and belongs in protected operational
storage. `GET /api/health` exposes only the migration ID, completion flag,
backup SHA-256, changed-operation count and payment-invariant result.

For an immediate full rollback, stop all application workers and import jobs,
verify the recorded backup hash, and restore that snapshot using SQLite's
backup API before starting the prior application revision. A full restore
replaces all subsequent database writes; reconcile later activity first if
the application has continued operating. Do not use the legacy `/api/upload-db`
endpoint for a live restore.

Validation: `python -m unittest test_contractor_identity -v`. This covers
backup contents and credentials, unchanged payment fields, metadata preservation,
idempotence, conflicting INNs, rollback on alias conflicts, same-name different-INN
separation, and preservation of manual categories during repeated imports.
