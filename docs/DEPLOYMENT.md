# Deployment and worker safety

The repository now has the executable pieces for a bounded
acquire-to-search path and a portable Airflow deployment bundle. The current
legacy estate is **not production-ready**, and no live worker or schedule has
yet been installed on the second computer.

```text
refresh-quarterly-estate sync --apply
                |
                v
estate.document.stored
                |
        per-consumer receipt
                |
    verified PDF/HTML/text -> Markdown
                |
                v
estate.document.parsed
                |
       optional Alpha Go projection
```

An estate commit is not proof of downstream delivery. Delivery health belongs
to the per-consumer receipt in `outbox_deliveries`.

## Read-only inspection

The deployment preflight opens SQLite with `mode=ro` and `query_only=ON`. It
never imports migration-owning estate classes.

```bash
# Observational report; audit mode always exits zero.
python3 scripts/deployment_preflight.py audit --json

# Enforce the worker policy; exits nonzero on error/blocker findings.
python3 scripts/deployment_preflight.py worker --json

# Enforce the full production policy.
python3 scripts/deployment_preflight.py production --json

# Include the intended Alpha Go targets in path validation.
python3 scripts/deployment_preflight.py production --json \
  --alpha-index alpha-go/data/index/alpha_go_expanded_hashing.db \
  --alpha-corpus alpha-go/data/corpus
```

Outbox status is also strictly read-only. It does not create the catalog,
subscriptions, or delivery tables:

```bash
python3 scripts/process_estate_outbox.py status \
  --database data/document_estate/catalog.db --json
```

`status` exits nonzero when the consumer schema is absent or active delivery
state has ready, expired, missing, or dead receipts. Future-scheduled retries
and disabled historical receipts are reported without being mistaken for
active failures. That is an operational finding, not permission to initialize
the live schema.

## Write boundary and consumers

`process-estate-outbox run` refuses to start without `--apply`. Starting it may
migrate acquisition/consumer tables, register subscriptions, backfill matching
historical events into receipts, write Markdown derivatives, emit parsed events,
and update an explicitly selected Alpha Go index.

The default derivative subscription is generated from its parser name and
version (`root.pdf-markdown.v1:<contract-hash>`):

- accepts `estate.document.stored` and routing/facet revision events;
- reads current project pins and memberships from the estate rather than
  trusting an old event payload;
- verifies the original bytes and catalogued SHA-256;
- parses PDFs and derives searchable Markdown directly from HTML/news or text
  originals;
- stores immutable content-addressed Markdown and parser-version lineage;
- emits `estate.document.parsed`;
- explicitly skips raw XBRL, which still needs a facts consumer.

Alpha Go projection is disabled by default. With `--enable-alpha-go`, the
target-specific `alpha-go.search-projection.v1:<target-hash>` subscription
accepts parsed events currently pinned to Alpha Go. It projects up to 64
receipts in one subprocess/model load and holds an Alpha-owned file lock around
manifest/index replacement. `--alpha-index` is required. A new target path gets
a new receipt generation automatically; pass and deliberately change
`--alpha-target-id` when rebuilding a target in place. Soft and Earnings
consumers are not implemented.

Do not use the following commands on the live estate until the rollout checklist
below has been completed:

```bash
# Register/migrate only; no delivery handler is called.
python3 scripts/process_estate_outbox.py run --apply \
  --estate-root data/document_estate --max-deliveries 0 --json

# Bounded root derivative batch.
python3 scripts/process_estate_outbox.py run --apply \
  --estate-root data/document_estate --max-deliveries 100 --json

# Bounded derivative and Alpha Go projection batch.
python3 scripts/process_estate_outbox.py run --apply \
  --estate-root data/document_estate --max-deliveries 100 \
  --enable-alpha-go \
  --alpha-index alpha-go/data/index/alpha_go_expanded_hashing.db \
  --alpha-target-id expanded-multilingual-v1 \
  --alpha-config alpha-go/configs/alpha_go.yaml --json
```

Claims are fenced, heartbeat-renewed, and reclaimable. One active batch per
consumer is allowed across workers, while different consumers can advance
independently. Retry delays and crash attempts are bounded. Every attempt is
retained in append-only history, and each enabled consumer has an independent
terminal receipt (`succeeded`, `skipped`, or `dead`). A dead receipt is terminal
for publication bookkeeping but remains a failure in status and preflight.
After fixing its cause, replay is an explicit write:

```bash
python3 scripts/process_estate_outbox.py requeue --apply \
  --database data/document_estate/catalog.db \
  --consumer-id '<exact generation-qualified consumer id>' \
  --limit 100 --json
```

## Container path excluded from this release

The supported second-computer deployment is native Python 3.13 plus native
Airflow processes; see
[`deployment/AIRFLOW_PORTABILITY.md`](deployment/AIRFLOW_PORTABILITY.md).
Container images are not part of this release gate and are not needed to run
the downloader. Any historical Docker files in the repository are optional
experiments, not the deployment contract.

For a greenfield or disposable estate, the Airflow bundle can initialize the
catalog through the owning acquisition CLI. See
[`deployment/AIRFLOW_PORTABILITY.md`](deployment/AIRFLOW_PORTABILITY.md) for
the native Python environment, double-gated freshness DAG, and second-computer
setup. Source coverage gaps remain reported but are not treated as a
prerequisite for testing the exportable stack.

## Controlled rollout checklist

1. Use Python 3.13.x (`.python-version` and the worker image pin 3.13.9).
2. Run the focused offline contract suite:

   ```bash
   python3 -m pytest -q \
     tests/test_consumer_outbox.py \
     tests/test_derivative_consumer.py \
     tests/test_alpha_go_consumer.py \
     tests/test_acquisition_to_alpha_e2e.py \
     tests/test_deployment_preflight.py
   ```

3. Run `deployment_preflight.py production`; preserve its JSON as a release
   artifact and resolve every blocker.
4. Back up the catalog and object store, then repeat the migration against a
   disposable estate copy. There is no automatic schema rollback command.
5. On that copy, run `status`, `run --apply --max-deliveries 0`, and preflight
   again. Review the new subscriptions and historical receipt count.
6. Process one root derivative, inspect its hash, lineage, compatibility path,
   parsed event, and retry behavior, then process a bounded batch.
7. Project into a disposable Alpha Go index and verify PDF and HTML news, a
   late Alpha project pin/facet revision, a corrected version, a repeated
   event, and a 100-event bounded batch without duplicate chunks.
8. Implement and certify the missing XBRL, Soft, and Earnings consumers.
9. Deploy a single non-overlapping worker with retained logs and alerting for
   retryable/dead receipts. Only then review an applied acquisition schedule.

## Current production blockers

A read-only production preflight on 2026-07-30 reports **7 blockers** in the
current workspace:

- only **25 of 179** active issuers have a primary PDF source
  (**154 missing**), and five bindings have explicit root live-canary
  evidence; 177 have quarterly BMV XBRL, with GFNorte and Tiendas 3B excluded;
- the live catalog has not been migrated to the acquisition, outbox, or
  consumer schemas;
- `content_objects.object_key` is absent from the live catalog;
- all **20,869** catalogued artifact paths are host-absolute compatibility
  paths;
- **699 of 3,087** original artifact rows have hashes not represented in
  `content_objects`.

The automated preflight does not replace the remaining product gates:

- issuer listing windows, active/delisted state, fiscal year ends, filing grace,
  and primary-source behavior are not issuer-certified;
- legacy Alpha Go, Soft, Earnings, root-report, and upload writers have not all
  been cut over to one durable estate writer;
- XBRL, Soft, and Earnings delivery consumers are missing;
- the production metadata/object-storage backend and cross-backend contract
  suite do not exist;
- no production worker, monitoring, backup/restore drill, or applied
  acquisition schedule has been deployed.

The same preflight confirms that all currently catalogued artifact and
content-object file paths resolve on this host. That local integrity result does
not remove the portability blockers.
