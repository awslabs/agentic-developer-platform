"""Real SQL authority, atomicity and byte-immutability checks on disposable databases.

Set DEVLAKE_POSTGRES_DSN to an isolated local PostgreSQL administrator. These
checks exercise SQL roles; AWS IAM/TLS acceptance remains in the live campaign.
"""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
POC = ROOT / "modules/insights/poc/devlake"
sys.path.insert(0, str(POC))
import cell_grants
import migration_receipt


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


@unittest.skipUnless(
    os.environ.get("DEVLAKE_POSTGRES_DSN"), "requires isolated PostgreSQL"
)
class CollectionSchemaPostgresTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import make_conninfo
        from psycopg import sql

        cls.pg = psycopg
        cls.sql = sql
        cls.manifest = migration_receipt.checked_manifest()
        cls.digest = hashlib.sha256(migration_receipt.SCHEMA.read_bytes()).hexdigest()
        suffix = uuid4().hex[:12]
        cls.database = "devlake_test_" + suffix
        cls.roles = {
            name: "devlake_" + name + "_" + suffix
            for name in ("migration", "writer", "reader", "foreign")
        }
        cls.admin = psycopg.connect(os.environ["DEVLAKE_POSTGRES_DSN"], autocommit=True)
        if not cls.admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'"
        ).fetchone():
            cls.admin.execute("CREATE ROLE rds_iam NOLOGIN")
        for role in cls.roles.values():
            cls.admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(sql.Identifier(role))
            )
            cls.admin.execute(
                sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(role))
            )
        cls.admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(cls.database), sql.Identifier(cls.roles["migration"])
            )
        )
        cls.dsns = {
            name: make_conninfo(
                os.environ["DEVLAKE_POSTGRES_DSN"], dbname=cls.database, user=role
            )
            for name, role in cls.roles.items()
        }
        cls.config = {
            "database_name": cls.database,
            "cell_id": "fixture-cell",
            "existing_db_resource_id": "cluster-FIXTURE123",
            "migration_username": cls.roles["migration"],
            "writer_db_username": cls.roles["writer"],
            "reader_db_username": cls.roles["reader"],
        }
        cls.migration_sql = (POC / "migrations/schema-v2.sql").read_text()
        for name, value in {
            "cell_id": "fixture-cell",
            "db_resource_id": "cluster-FIXTURE123",
            "manifest_sha256": cls.digest,
        }.items():
            cls.migration_sql = cls.migration_sql.replace(
                ":'" + name + "'", sql.Literal(value).as_string(cls.admin)
            )
        with psycopg.connect(cls.dsns["migration"]) as connection:
            connection.execute(cls.migration_sql)
            connection.execute(
                cell_grants.checked_roles(cls.config, cls.manifest, cls.digest)
            )

    @classmethod
    def tearDownClass(cls):
        cls.admin.execute(
            cls.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                cls.sql.Identifier(cls.database)
            )
        )
        for role in cls.roles.values():
            cls.admin.execute(
                cls.sql.SQL("DROP ROLE {}").format(cls.sql.Identifier(role))
            )
        cls.admin.close()

    def setUp(self):
        with self.pg.connect(self.dsns["migration"]) as c:
            names = (
                self.manifest["raw"]["tables"]
                + self.manifest["tool"]
                + self.manifest["domain"]
            )
            c.execute("TRUNCATE " + ", ".join("public." + name for name in names))
            c.execute(
                "TRUNCATE devlake_meta.collection_generations, devlake_meta.collection_batch_manifests, devlake_meta.collection_checkpoints, devlake_meta.native_connections CASCADE"
            )

    def begin(self, c, expected=0, attempt="attempt-one"):
        return c.execute(
            "SELECT devlake_meta.begin_collection('fixture-cell', %s, 'tenant-one', 'project-one', 'operation-one', %s)",
            (expected, attempt),
        ).fetchone()[0]

    def batch(self, generation=1, native=b'{ "source": "exact bytes" }'):
        key = dict(
            CellID="fixture-cell",
            TenantID="tenant-one",
            ProjectID="project-one",
            Tool="github",
            ConnectionID="connection-one",
            Scope="repo-one",
            Kind="issue",
            NativeID="42",
        )
        item = dict(
            Key=key,
            Revision="r1",
            NativeBytes=base64.b64encode(native).decode(),
            NormalizedBytes=base64.b64encode(b'{"id":"github:42"}').decode(),
            Links=None,
            PolicyVersion="p1",
            FieldMapVersion="f1",
            SchemaVersion="s1",
        )
        payload = encoded(item)
        batch_id = str(uuid4())
        manifest = encoded(
            dict(
                BatchID=batch_id,
                CellID="fixture-cell",
                Generation=generation,
                ItemDigests=[dict(Key=key, SHA256=hashlib.sha256(payload).hexdigest())],
            )
        )
        return batch_id, [payload], manifest

    def seal(self, c, batch, generation=1, checkpoints=None):
        return bytes(
            c.execute(
                "SELECT devlake_meta.seal_batch('fixture-cell', %s, %s, %s, %s, %s::jsonb)",
                (generation, *batch, json.dumps(checkpoints or [])),
            ).fetchone()[0]
        )

    def test_expanded_native_seal_preserves_raw_origin_and_rejects_invented_links(self):
        providers = ("sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear", "slack")

        def native_batch(native_ids, bad_origin=False, invented_link=False, generation=1, previous=None):
            batch_id = str(uuid4())
            keys, payloads, source_keys, updates = [], [], [], []
            for provider in providers:
                key = dict(CellID="fixture-cell", TenantID="tenant-one", ProjectID="project-one",
                           Tool=provider, ConnectionID="credential-opaque", Scope="selected", Kind="native_row",
                           NativeID=f"{provider}:Model:{native_ids[provider]}:7")
                source_key = encoded({field: key[field] for field in
                                      ("CellID", "TenantID", "ProjectID", "Tool", "ConnectionID", "Scope")}).decode()
                origin = dict(_raw_data_table=f"_raw_{provider}_api_record", _raw_data_id=7,
                              _raw_data_params=json.dumps({"connectionId": native_ids[provider] + 1 if bad_origin else native_ids[provider]}))
                links = ([dict(From=key, To=key, Relation="source-reference", NativeEvidenceID="status")]
                         if invented_link else None)
                item = dict(Key=key, Revision="native-r7", NativeConnectionID=native_ids[provider], RawDataOrigin=origin,
                            NativeBytes=base64.b64encode(b'{"status":"raw-failure"}').decode(),
                            NormalizedBytes=base64.b64encode(encoded({"id": key["NativeID"],
                                                                         "OriginalStatus": "raw-failure"})).decode(),
                            Links=links, PolicyVersion="policy-7", FieldMapVersion="devlake-pin",
                            SchemaVersion="schema-v2")
                keys.append(dict(Key=key, SHA256=hashlib.sha256(encoded(item)).hexdigest()))
                payloads.append(encoded(item))
                source_keys.append(source_key)
                updates.append(dict(source_key=source_key, expected_cursor=previous, next_cursor="complete"))
            bindings = {key: dict(ID=key, Generation=1, Revision=1) for key in source_keys}
            manifest = encoded(dict(BatchID=batch_id, CellID="fixture-cell", Generation=generation,
                                    ItemDigests=keys, SourceKeys=sorted(source_keys), SourceBindings=bindings))
            return (batch_id, payloads, manifest), updates

        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
            native_ids = {
                provider: writer.execute(
                    "SELECT devlake_meta.native_connection('fixture-cell', 'tenant-one', 'project-one', %s, 'credential-opaque', 1)",
                    (provider,),
                ).fetchone()[0] for provider in providers
            }
            self.assertEqual(len(set(native_ids.values())), len(providers))
            self.assertEqual(native_ids["jenkins"], writer.execute(
                "SELECT devlake_meta.native_connection('fixture-cell', 'tenant-one', 'project-one', 'jenkins', 'credential-opaque', 1)"
            ).fetchone()[0])
            for scope in (("foreign", "tenant-one", "project-one"),
                          ("fixture-cell", "foreign", "project-one"),
                          ("fixture-cell", "tenant-one", "foreign")):
                with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                    writer.execute(
                        "SELECT devlake_meta.native_connection(%s, %s, %s, 'jenkins', 'credential-opaque', 1)", scope
                    )
            for bad_origin, invented_link in ((True, False), (False, True)):
                batch, updates = native_batch(native_ids, bad_origin, invented_link)
                with self.assertRaises(self.pg.errors.RaiseException):
                    self.seal(writer, batch, checkpoints=updates)
            batch, updates = native_batch(native_ids)
            self.assertEqual(self.seal(writer, batch, checkpoints=updates),
                             hashlib.sha256(batch[2]).digest())
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                writer.execute(
                    "SELECT devlake_meta.native_connection('fixture-cell', 'tenant-one', 'project-one', 'jenkins', 'credential-opaque', 1)"
                )
            self.begin(writer, expected=1, attempt="attempt-two")
            second_ids = {
                provider: writer.execute(
                    "SELECT devlake_meta.native_connection('fixture-cell', 'tenant-one', 'project-one', %s, 'credential-opaque', 2)",
                    (provider,),
                ).fetchone()[0] for provider in providers
            }
            self.assertTrue(set(native_ids.values()).isdisjoint(second_ids.values()))
            stale_batch, updates = native_batch(native_ids, generation=2, previous="complete")
            with self.assertRaises(self.pg.errors.RaiseException):
                self.seal(writer, stale_batch, checkpoints=updates, generation=2)
            fresh_batch, updates = native_batch(second_ids, generation=2, previous="complete")
            self.assertEqual(self.seal(writer, fresh_batch, checkpoints=updates, generation=2),
                             hashlib.sha256(fresh_batch[2]).digest())
        with self.pg.connect(self.dsns["reader"], autocommit=True) as reader:
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                reader.execute(
                    "SELECT devlake_meta.native_connection('fixture-cell', 'tenant-one', 'project-one', 'jenkins', 'credential-opaque', 1)"
                )
        with self.pg.connect(self.dsns["reader"]) as reader:
            rows = reader.execute(
                "SELECT native_payload, normalized_payload FROM devlake_meta.collection_batch_items WHERE batch_id=%s",
                (batch[0],),
            ).fetchall()
            self.assertEqual(len(rows), 7)
            for native, normalized in rows:
                self.assertEqual(bytes(native), b'{"status":"raw-failure"}')
                self.assertEqual(json.loads(normalized)["OriginalStatus"], "raw-failure")

    def test_migration_replay_and_actual_role_boundaries(self):
        with self.pg.connect(self.dsns["migration"]) as c:
            c.execute(self.migration_sql)
            c.execute(
                cell_grants.checked_roles(self.config, self.manifest, self.digest)
            )
        with self.pg.connect(self.dsns["writer"], autocommit=True) as c:
            self.assertEqual(
                c.execute(
                    "SELECT manifest_sha256 FROM devlake_meta.schema_receipts"
                ).fetchone()[0],
                self.digest,
            )
            for statement in (
                "CREATE TABLE public.escape(id int)",
                "CREATE TEMP TABLE escape(id int)",
                "ALTER TABLE public.issues ADD COLUMN escape text",
                "DROP TABLE public.issues",
                "TRUNCATE public.issues",
                "UPDATE devlake_meta.collection_generations SET generation=99",
                "SET ROLE " + self.roles["migration"],
            ):
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(self.pg.errors.InsufficientPrivilege),
                ):
                    c.execute(statement)
        with self.pg.connect(self.dsns["reader"], autocommit=True) as c:
            self.assertEqual(
                c.execute("SELECT count(*) FROM public.issues").fetchone()[0], 0
            )
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                self.begin(c)
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                c.execute("INSERT INTO public.issues(id) VALUES ('x')")
        with self.assertRaises(self.pg.OperationalError):
            self.pg.connect(self.dsns["foreign"])

    def test_guards_cover_all_upstream_tables_and_new_scopes_need_no_ddl(self):
        with self.pg.connect(self.dsns["writer"], autocommit=True) as c:
            for name in (
                self.manifest["raw"]["tables"]
                + self.manifest["tool"]
                + self.manifest["domain"]
            ):
                with (
                    self.subTest(table=name),
                    self.assertRaises(self.pg.errors.InsufficientPrivilege),
                ):
                    c.execute("INSERT INTO public." + name + " DEFAULT VALUES")
            self.assertEqual(self.begin(c), 1)
            with c.transaction():
                c.execute("SELECT devlake_meta.lock_generation('fixture-cell', 1)")
                c.execute(
                    "INSERT INTO public._raw_github_api_issues(params,data) VALUES ('scope-one', %s), ('new-scope', %s)",
                    (b"first", b"second"),
                )
                c.execute(
                    "INSERT INTO public.issues(id,title) VALUES ('github:42', 'Github'), ('jira:42','Jira')"
                )
            self.assertEqual(
                c.execute("SELECT count(*) FROM public.issues").fetchone()[0], 2
            )
            self.assertEqual(self.begin(c, 1, "attempt-two"), 2)
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                c.execute("SELECT devlake_meta.lock_generation('fixture-cell', 1)")
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                c.execute(
                    "SELECT devlake_meta.begin_collection('other-cell', 0, 't','p','o','a')"
                )

    def test_seal_is_atomic_checkpointed_and_preserves_exact_bytes(self):
        batch = self.batch()
        updates = [
            {
                "source_key": "github:connection-one:repo-one",
                "expected_cursor": None,
                "next_cursor": "page-2",
            }
        ]
        with self.pg.connect(self.dsns["writer"], autocommit=True) as c:
            self.begin(c)
            digest = self.seal(c, batch, checkpoints=updates)
            self.assertEqual(digest, hashlib.sha256(batch[2]).digest())
            self.assertEqual(self.seal(c, batch, checkpoints=updates), digest)
            self.assertEqual(
                c.execute(
                    "SELECT cursor_value FROM devlake_meta.collection_checkpoints"
                ).fetchone()[0],
                "page-2",
            )
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                c.execute("INSERT INTO public.issues(id) VALUES ('late')")
            for table in (
                "collection_batch_items",
                "collection_batch_manifests",
                "collection_checkpoints",
            ):
                with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                    c.execute("DELETE FROM devlake_meta." + table)
            self.begin(c, 1, "attempt-two")
            with c.transaction():
                c.execute("SELECT devlake_meta.lock_generation('fixture-cell', 2)")
                c.execute("DELETE FROM public.issues")
            second = self.batch(2, b"changed")
            with self.assertRaises(self.pg.errors.SerializationFailure):
                self.seal(
                    c,
                    second,
                    2,
                    [
                        {
                            "source_key": updates[0]["source_key"],
                            "expected_cursor": "wrong",
                            "next_cursor": "lost",
                        }
                    ],
                )
            self.assertEqual(
                c.execute(
                    "SELECT state FROM devlake_meta.collection_generations"
                ).fetchone()[0],
                "collecting",
            )
            with self.pg.connect(self.dsns["reader"]) as reader:
                self.assertEqual(
                    reader.execute(
                        "SELECT count(*) FROM devlake_meta.collection_batch_manifests"
                    ).fetchone()[0],
                    1,
                )
            self.seal(
                c,
                second,
                2,
                [
                    {
                        "source_key": updates[0]["source_key"],
                        "expected_cursor": "page-2",
                        "next_cursor": "page-3",
                    }
                ],
            )
        with self.pg.connect(self.dsns["reader"]) as c:
            row = c.execute(
                "SELECT item_payload, native_payload, normalized_payload FROM devlake_meta.collection_batch_items WHERE batch_id=%s",
                (batch[0],),
            ).fetchone()
            self.assertEqual(
                tuple(bytes(x) for x in row),
                (batch[1][0], b'{ "source": "exact bytes" }', b'{"id":"github:42"}'),
            )
            self.assertEqual(
                c.execute(
                    "SELECT count(*) FROM devlake_meta.collection_batch_manifests"
                ).fetchone()[0],
                2,
            )

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_COLLECTION_TEST_BINARY"), "requires pinned collection test binary")
    def test_go_incremental_removal_adapter(self):
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_COLLECTION_TEST_BINARY"], "-test.run=^TestPostgresIncrementalRemoval$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"],
                 "DEVLAKE_S03_READER_DSN": self.dsns["reader"], "DEVLAKE_S03_MANIFEST_DIGEST": self.digest},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_WORKER_TEST_BINARY"), "requires pinned worker executable test binary")
    def test_go_worker_entry_point(self):
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_WORKER_TEST_BINARY"], "-test.run=^TestPostgresWorkerEntryPoint$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"],
                 "DEVLAKE_S03_READER_DSN": self.dsns["reader"], "DEVLAKE_S03_SCHEMA_PATH": str(POC / "schema-manifest.json")},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"), "requires pinned native worker test binary")
    def test_go_native_worker_conversion_and_sealed_readback(self):
        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"], "-test.run=^TestPostgresNativeWorkerConversion$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"], "DEVLAKE_S03_READER_DSN": self.dsns["reader"]},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"), "requires pinned native worker test binary")
    def test_go_native_worker_run_with_scoped_pages(self):
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"], "-test.run=^TestPostgresNativeRun$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"], "DEVLAKE_S03_READER_DSN": self.dsns["reader"],
                 "DEVLAKE_S03_SCHEMA_PATH": str(POC / "schema-manifest.json")},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"), "requires pinned native worker test binary")
    def test_go_native_malformed_record_rolls_back(self):
        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"], "-test.run=^TestPostgresMalformedNativeRollback$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"], "DEVLAKE_S03_READER_DSN": self.dsns["reader"]},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.environ.get("DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"), "requires pinned native worker test binary")
    def test_go_pagerduty_native_conversion(self):
        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
        result = subprocess.run(
            [os.environ["DEVLAKE_S03_NATIVE_WORKER_TEST_BINARY"], "-test.run=^TestPostgresPagerDutyConversion$", "-test.v"],
            env={**os.environ, "DEVLAKE_S03_WRITER_DSN": self.dsns["writer"]},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_payload_contract_upgrade_preserves_old_sealed_bytes(self):
        first = self.batch()
        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
            self.seal(writer, first)
        with self.pg.connect(self.dsns["migration"]) as migration:
            migration.execute("UPDATE devlake_meta.schema_receipts SET manifest_sha256=%s",
                              ("db902ed429de0cd914efe352bb192bab72bcb410b6441fef8a8e207d7554b8d6",))
            migration.execute("ALTER TABLE devlake_meta.collection_batch_manifests DROP CONSTRAINT collection_batch_manifests_item_count_check")
            migration.execute("ALTER TABLE devlake_meta.collection_batch_manifests ADD CONSTRAINT collection_batch_manifests_item_count_check CHECK (item_count BETWEEN 1 AND 100000)")
            migration.execute(self.migration_sql)
            self.assertEqual(migration.execute("SELECT manifest_sha256 FROM devlake_meta.schema_receipts").fetchone()[0], self.digest)
        with self.pg.connect(self.dsns["reader"]) as reader:
            self.assertEqual(bytes(reader.execute("SELECT item_payload FROM devlake_meta.collection_batch_items").fetchone()[0]), first[1][0])
            self.assertEqual(bytes(reader.execute("SELECT manifest_payload FROM devlake_meta.collection_batch_manifests").fetchone()[0]), first[2])
        with self.pg.connect(self.dsns["migration"], autocommit=True) as migration:
            with migration.transaction():
                migration.execute("UPDATE devlake_meta.schema_receipts SET manifest_sha256=%s", ("f" * 64,))
                with self.assertRaises(self.pg.Error):
                    with migration.transaction():
                        migration.execute(self.migration_sql)
                migration.execute("UPDATE devlake_meta.schema_receipts SET manifest_sha256=%s", (self.digest,))

    def test_non_native_removal_empty_result_and_checkpoint_recovery(self):
        first = self.batch()
        original = json.loads(first[1][0])
        source = {key: value for key, value in original["Key"].items() if key not in ("Kind", "NativeID")}
        source_key = encoded(source).decode()
        updates = [{"source_key": source_key, "expected_cursor": None, "next_cursor": "page-1"}]
        with self.pg.connect(self.dsns["writer"], autocommit=True) as writer:
            self.begin(writer)
            self.seal(writer, first, checkpoints=updates)
            self.begin(writer, 1, "attempt-two")
            removal = {**original, "RemovalReason": "policy_excluded", "Revision": "",
                       "NativeBytes": None, "NormalizedBytes": None, "Links": None}
            second_id = str(uuid4())

            def removal_batch(item):
                payload = encoded(item)
                manifest = encoded({"BatchID": second_id, "CellID": "fixture-cell", "Generation": 2,
                                    "ItemDigests": [{"Key": item["Key"], "SHA256": hashlib.sha256(payload).hexdigest()}],
                                    "SourceKeys": [source_key]})
                return second_id, [payload], manifest

            second = removal_batch(removal)
            next_updates = [{"source_key": source_key, "expected_cursor": "page-1", "next_cursor": "page-2"}]
            for invalid in ({**removal, "NativeBytes": original["NativeBytes"]},
                            {**removal, "Revision": "invented"},
                            {**removal, "RemovalReason": "assumed"}):
                with self.assertRaises(self.pg.Error):
                    self.seal(writer, removal_batch(invalid), 2, next_updates)
            with self.assertRaises(self.pg.errors.SerializationFailure):
                self.seal(writer, second, 2, [{**next_updates[0], "expected_cursor": "wrong"}])
            self.assertEqual(writer.execute("SELECT cursor_value FROM devlake_meta.collection_checkpoints").fetchone()[0], "page-1")
            with self.pg.connect(self.dsns["reader"]) as reader:
                self.assertEqual(reader.execute("SELECT count(*) FROM devlake_meta.collection_batch_manifests").fetchone()[0], 1)
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                with writer.transaction():
                    self.seal(writer, second, 2, next_updates)
                    raise RuntimeError("simulated crash")
            digest = self.seal(writer, second, 2, next_updates)
            self.assertEqual(self.seal(writer, second, 2, next_updates), digest)
            with self.assertRaises(self.pg.Error):
                self.seal(writer, removal_batch({**removal, "RemovalReason": "absent_in_complete_source"}), 2, next_updates)
            self.begin(writer, 2, "attempt-three")
            empty_id = str(uuid4())
            empty_manifest = {"BatchID": empty_id, "CellID": "fixture-cell", "Generation": 3,
                              "ItemDigests": [], "SourceKeys": [source_key]}
            empty = (empty_id, [], encoded(empty_manifest))
            empty_updates = [{"source_key": source_key, "expected_cursor": "page-2", "next_cursor": "page-3"}]
            with self.assertRaises(self.pg.Error):
                self.seal(writer, (empty_id, [], encoded({**empty_manifest, "SourceKeys": []})), 3, empty_updates)
            self.assertEqual(self.seal(writer, empty, 3, empty_updates), hashlib.sha256(empty[2]).digest())
            self.assertEqual(self.seal(writer, empty, 3, empty_updates), hashlib.sha256(empty[2]).digest())
            self.assertEqual(writer.execute("SELECT cursor_value FROM devlake_meta.collection_checkpoints").fetchone()[0], "page-3")
        with self.pg.connect(self.dsns["reader"]) as reader:
            before = reader.execute("SELECT item_payload FROM devlake_meta.collection_batch_items WHERE batch_id=%s", (first[0],)).fetchone()
            self.assertEqual(bytes(before[0]), first[1][0])
            removed = reader.execute("SELECT item_payload, native_payload, normalized_payload FROM devlake_meta.collection_batch_items WHERE batch_id=%s", (second_id,)).fetchone()
            self.assertEqual(tuple(bytes(value) for value in removed), (second[1][0], b"", b""))
            self.assertEqual(reader.execute("SELECT count(*) FROM devlake_meta.collection_batch_items WHERE batch_id=%s", (empty_id,)).fetchone()[0], 0)

    def test_bad_digest_duplicate_or_foreign_identity_never_seals(self):
        with self.pg.connect(self.dsns["writer"], autocommit=True) as c:
            self.begin(c)
            for variant in (
                "digest",
                "duplicate",
                "tenant",
                "link",
                "link_identity",
                "link_duplicate",
            ):
                batch_id, payloads, manifest = self.batch()
                if variant == "digest":
                    payloads[0] += b" "
                if variant == "duplicate":
                    payloads *= 2
                    m = json.loads(manifest)
                    m["ItemDigests"] *= 2
                    manifest = encoded(m)
                if variant in ("tenant", "link", "link_identity", "link_duplicate"):
                    doc = json.loads(payloads[0])
                    if variant == "tenant":
                        doc["Key"]["TenantID"] = "foreign"
                    else:
                        doc["Links"] = [
                            dict(
                                From=doc["Key"],
                                To={
                                    **doc["Key"],
                                    **(
                                        {"ProjectID": "foreign"}
                                        if variant == "link"
                                        else {"NativeID": ""}
                                        if variant == "link_identity"
                                        else {}
                                    ),
                                },
                                Relation="source-reference",
                                NativeEvidenceID="native-link",
                            )
                        ]
                    if variant == "link_duplicate":
                        doc["Links"] *= 2
                    payloads[0] = encoded(doc)
                    m = json.loads(manifest)
                    m["ItemDigests"] = [
                        dict(
                            Key=doc["Key"],
                            SHA256=hashlib.sha256(payloads[0]).hexdigest(),
                        )
                    ]
                    manifest = encoded(m)
                with self.subTest(variant=variant), self.assertRaises(self.pg.Error):
                    self.seal(c, (batch_id, payloads, manifest))
            with self.pg.connect(self.dsns["reader"]) as reader:
                self.assertEqual(
                    reader.execute(
                        "SELECT count(*) FROM devlake_meta.collection_batch_manifests"
                    ).fetchone()[0],
                    0,
                )
            with self.assertRaises(self.pg.Error):
                bounded = self.batch()
                self.seal(c, (bounded[0], [bounded[1][0]] * 100001, bounded[2]))
            c.execute("SELECT devlake_meta.cancel_collection('fixture-cell', 1)")
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                self.seal(c, self.batch())
            with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                self.begin(c)

    def test_successor_waits_for_workspace_transaction_then_fences_it(self):
        with (
            self.pg.connect(self.dsns["writer"], autocommit=True) as first,
            self.pg.connect(self.dsns["writer"], autocommit=True) as second,
        ):
            self.begin(first)
            with first.transaction():
                first.execute("SELECT devlake_meta.lock_generation('fixture-cell', 1)")
                first.execute(
                    "INSERT INTO public.issues(id,title) VALUES ('first','held')"
                )
                second.execute("SET lock_timeout = '100ms'")
                with self.assertRaises(self.pg.errors.LockNotAvailable):
                    self.begin(second, 1, "attempt-two")
            self.assertEqual(self.begin(second, 1, "attempt-two"), 2)
            with first.transaction():
                first.execute(
                    "SELECT set_config('devlake.cell_id','fixture-cell',true), set_config('devlake.generation','1',true)"
                )
                with self.assertRaises(self.pg.errors.InsufficientPrivilege):
                    first.execute("UPDATE public.issues SET title='stale'")
            self.assertEqual(
                first.execute(
                    "SELECT title FROM public.issues WHERE id='first'"
                ).fetchone()[0],
                "held",
            )


if __name__ == "__main__":
    unittest.main()
