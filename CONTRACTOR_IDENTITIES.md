# Whole-directory counterparty consolidation

Startup now processes the complete database rather than a two-name list.
Migration v1 is preserved; v2 creates its own complete SQLite backup before
writing, with owner-only permissions, integrity_check and SHA-256. The full
server-only report is retained in contractor_merge_log.

Migration, directory lookup and imports share these identity rules:

- A valid identical INN identifies one entity, including changed bank labels.
- Full legal names ignore case, whitespace, quotes, dots, legal-form expansions
  and е/ё. An unqualified name links to its only observed valid INN.
- Two different INNs remain separate. A new bare name shared by two INNs is
  unresolved; neither old aliases nor row order can override that ambiguity.
- An INN fragment must match the known prefix. Invalid full INNs establish no link.
- Legacy 60-character truncation needs a unique continuation. Different company
  suffixes and similar surnames are never fuzzy-merged.
- Complete personal beneficiary names in structured // bank labels can match
  the same three-part name with the IP prefix.
- The known Standard-Star missing-letter legacy variant is included in that
  counterparty's original reviewed consolidation.

Names with no complete INN can still share one canonical label and a stable
name identity. No INN is invented. Original labels remain on every operation.
Source metadata rows and comments remain available. Conflicting category
defaults never change the individual payment categories.

Every month is compared before commit: all fields except identity must stay
identical, including counts, amounts, dates, descriptions, categories, flags,
ordering, metadata and stored aggregates. Failure rolls back the transaction
and keeps the backup. Restarts are idempotent.

Health exposes only the backup hash and aggregate counts. Deployment also
checks the actual directory for compatible duplicate keys and verifies common
histories through the old and canonical names of Grachev, Paraev and Sirotkin.
No database or payment rows are uploaded to GitHub.

Run: python -m unittest discover -p 'test*contractor*.py' -v

For full rollback, stop database writers, verify the backup hash, and restore
the snapshot using SQLite's backup API before restarting the prior source.
Reconcile subsequent writes before a full restore if the app kept operating.

