"""Mounted-home checkpoint contract using a pinned Crew 0.11.2-shaped fixture."""

import ast
import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import crew_checkpoint
from crew_checkpoint import InvalidCheckpoint, capture, restore, restore_empty_mount, restore_exact

GATEWAY_RULES = Path(__file__).resolve().parents[4] / "modules" / "gateway" / "src" / "crew" / "checkpoint_objects.py"


def gateway_inventory_rules():
    """The gateway's archive inventory constants, read from source so this test needs no gateway deps."""
    tree = ast.parse(GATEWAY_RULES.read_text())
    wanted = {"ALLOWED_FILES", "ALLOWED_TREES", "EXCLUDED_NAMES", "EXCLUDED_SUFFIXES"}
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and getattr(node.targets[0], "id", None) in wanted:
            values[node.targets[0].id] = _literal(node.value)
    assert wanted <= set(values), values.keys()
    return values


def _literal(node):
    if isinstance(node, ast.Call):  # frozenset({...})
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def gateway_valid_path(name):
    """Re-states checkpoint_objects.valid_path from the gateway's constants (same rule shape)."""
    rules = gateway_inventory_rules()
    parts = PurePosixPath(name).parts
    allowed = rules["ALLOWED_FILES"] | rules["ALLOWED_TREES"]
    return not (
        not parts or str(PurePosixPath(name)) != name or name.startswith("/") or "\\" in name
        or any(part in (".", "..") for part in parts)
        or parts[0] not in allowed
        or parts[0] in rules["ALLOWED_FILES"] and len(parts) != 1
        or parts[0] == ".kiro" and parts[:3] != (".kiro", "sessions", "cli")
        or any(part in rules["EXCLUDED_NAMES"] or part.endswith(tuple(rules["EXCLUDED_SUFFIXES"])) for part in parts)
    )

class CrewCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "mounted-home"
        self.home.mkdir()
        self.archive = self.root / "scratch.tar"
        self.manifest = self.root / "scratch.json"
        self.fixture = {
            "workspace/memory/preferences.md": b"preferred: concise\n",
            "lessons.jsonl": b'{"provenance":"explicit-v1"}\n',
            "memory_stores/analyst/lessons.jsonl": b'{"provenance":"explicit"}\n',
            "members/analyst/briefing.md": b"approved briefing\n",
            "trust/member-rules/analyst.md": b"required approval\n",
            "sessions/conversation.jsonl": b'{"turn":"hello"}\n',
            "adp-crew-session-map/1234.json": b'{"sessionId":"adp-one","target":"crew-one"}\n',
            "artifacts/report.txt": b"approved output\n",
            ".kiro/sessions/cli/turn.jsonl": b"cli conversation\n",
        }
        for name, data in self.fixture.items():
            destination = self.home / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        for name in ("memory.db", "memory_stores/analyst/memory.db"):
            destination = self.home / name
            with closing(sqlite3.connect(destination)) as db:
                db.execute("CREATE TABLE memory (revision INTEGER, provenance TEXT)")
                db.execute("INSERT INTO memory VALUES (2, 'explicit')")
                db.commit()
        (self.home / "config.json").write_text('{"credential":"managed"}')
        (self.home / "trust" / "sel_hmac.key").write_bytes(b"managed-secret")
        # The Dev Box host's root-only provisioning marker (restore_crew_host.py) is box-bound
        # and unreadable by the agent: it must never enter (or fail) a capture.
        (self.home / ".adp-host").mkdir(mode=0o700)
        (self.home / ".adp-host" / "provision.json").write_text('{"box_id":"this-box"}')
        (self.home / ".adp").mkdir(mode=0o700)  # the adp mcp shim's operation ledger (#8380)
        (self.home / ".adp" / "operations.json").write_text('{"operations":[]}')

    def captured(self):
        manifest = capture(self.home, self.archive, self.manifest, quiesced=True,
                           quiescence=threading.Lock())
        archive = self.archive.read_bytes()
        manifest_bytes = self.manifest.read_bytes()
        receipt = {"archive_sha256": hashlib.sha256(archive).hexdigest(),
                   "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
        return manifest, archive, manifest_bytes, receipt

    def test_restore_complete_home_after_volume_loss_without_managed_secrets(self):
        manifest, archive, manifest_bytes, receipt = self.captured()
        self.assertTrue(manifest["quiesced"])
        self.assertIn("config.json", manifest["excluded"])
        self.assertIn("trust/sel_hmac.key", manifest["excluded"])
        self.assertIn(".adp-host", manifest["excluded"])
        self.assertIn(".adp", manifest["excluded"])
        self.assertNotIn(b"this-box", archive)
        self.assertNotIn(b"managed-secret", archive)
        self.assertNotIn(b"managed", archive)
        new_home = self.root / "replacement-volume" / "crew"
        new_home.parent.mkdir()
        restore(new_home, archive, manifest_bytes, **receipt)
        for name, data in self.fixture.items():
            self.assertEqual((new_home / name).read_bytes(), data)
        self.assertFalse((new_home / "config.json").exists())
        self.assertFalse((new_home / "trust/sel_hmac.key").exists())
        for name in ("memory.db", "memory_stores/analyst/memory.db"):
            with closing(sqlite3.connect(new_home / name)) as db:
                self.assertEqual(db.execute("SELECT * FROM memory").fetchone(), (2, "explicit"))

    def test_unknown_entries_and_symlinks_inside_owned_trees_are_skipped_and_named_never_followed(self):
        # #8223 cause 37: a real Kiro Crew home carries ~60 top-level entries this inventory does not
        # know; an unknown entry is reported, never fatal. A symlink inside an OWNED tree is skipped and
        # named too (dev 2026-10-26: Codex's apply_patch links under .codex/tmp had failed every backup
        # for a day); the archive still never follows anything out of the home.
        (self.home / "unknown.dat").write_text("not classified")
        (self.home / "unknown-tree").mkdir()
        (self.home / "unknown-tree" / "outside").symlink_to(self.root)
        _, _, manifest_bytes, _ = self.captured()
        manifest = json.loads(manifest_bytes)
        self.assertIn("unknown.dat", manifest["excluded"])
        self.assertIn("unknown-tree", manifest["excluded"])
        self.assertEqual(manifest["excluded_reasons"]["unknown.dat"], "unclassified")
        self.assertNotIn("unknown-tree/outside", manifest["excluded"])
        (self.home / "workspace" / "outside").symlink_to(self.root)
        (self.home / ".codex" / "tmp" / "arg0-linux" / "codex-arg0x").mkdir(parents=True)
        (self.home / ".codex" / "tmp" / "arg0-linux" / "codex-arg0x" / "apply_patch").symlink_to("/opt/adp/crew/current/clients/codex")
        manifest, archive_bytes, _, _ = self.captured()
        self.assertEqual(manifest["excluded_reasons"]["workspace/outside"], "runtime")
        self.assertEqual(manifest["excluded_reasons"][".codex/tmp"], "runtime")
        self.assertNotIn(".codex/tmp/arg0-linux", manifest["excluded"])
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            names = archive.getnames()
        self.assertFalse(any(name.startswith(("workspace/outside", ".codex/tmp")) for name in names), names)

    def test_real_kiro_crew_home_layout_captures_owner_data_and_skips_machine_state(self):
        # The exact top-level listing of a provisioned Dev Box home (dev 2026-10-21, gen 25) plus
        # the .kiro children; every entry is either preserved, excluded by name, or skipped.
        listing = """.adp-host .cache .claude .claude.json .crons.lock .kiro .kiro_cli_setup_complete .local_secret
        .migrations .settings_seeds.lock admission_policy.json agent_model_state.json agent_model_state.json.lock
        appearance-library apps artifacts auth-store-staging autonudge.json aws-control-staging aws_service_consent.json
        backups cache cloud.json cloud_launch_state.json computer_use.json config.json config.json.bak config.json.lock
        config.local.json connections connections_ui_migrated.json credential_redaction.json crew-log crew-panels
        crew-teams crewmate_prune.lock crewmate_prune_v3_migrated.json cron-history decisions decisions_consent.json
        diag file-delivery-consent-pending file_delivery_consent.json gateway.lock gateway.log gateway.log.prev
        inbound-spool kiro_pids.lock kiro_pids.txt kiro_session_pids.lock kiro_session_pids.txt live-target-staging
        live_target.json logs mcp""".split()  # noqa: SIM905 — the verbatim listing reads better than a 60-item list literal
        directories = {".adp-host", ".cache", ".claude", ".kiro", ".migrations", "appearance-library", "apps", "artifacts",
                       "auth-store-staging", "aws-control-staging", "backups", "cache", "connections", "crew-log",
                       "crew-panels", "crew-teams", "cron-history", "decisions", "diag", "file-delivery-consent-pending",
                       "inbound-spool", "live-target-staging", "logs", "mcp"}
        for name in listing:
            target = self.home / name
            if target.exists() or target.is_symlink():
                continue
            if name in directories:
                target.mkdir()
                (target / "entry.json").write_text("{}")
            else:
                target.write_text("x")
        (self.home / ".local_secret").write_bytes(b"owner-local-secret")
        (self.home / "connections" / "github.json").write_text('{"connection":"owner"}')
        (self.home / "decisions" / "d1.json").write_text('{"decision":"owner"}')
        for child in ("agents", "settings", "crew-auth-staging", "unknown-child"):
            (self.home / ".kiro" / child).mkdir(exist_ok=True)
            (self.home / ".kiro" / child / "entry.json").write_text("{}")
        socket_path = self.home / "dashboard-5476.sock"
        import socket
        with closing(socket.socket(socket.AF_UNIX)) as sock:
            sock.bind(str(socket_path))
            _, _, manifest_bytes, _ = self.captured()
        manifest = json.loads(manifest_bytes)
        files = set(manifest["files"])
        for kept in ("artifacts/report.txt", "crew-log/entry.json", "crew-teams/entry.json", "memory.db", "lessons.jsonl",
                     ".kiro/sessions/cli/turn.jsonl"):
            self.assertIn(kept, files, kept)
        for never in (".local_secret", "token_signing.key", "trust/sel_hmac.key", ".claude.json", "config.json",
                      "live_target.json", "cloud_launch_state.json", "admission_policy.json", "gateway.log",
                      "kiro_pids.txt", "dashboard-5476.sock", ".kiro/agents/entry.json", ".kiro/settings/entry.json",
                      ".kiro/crew-auth-staging/entry.json", ".kiro/unknown-child/entry.json",
                      "connections/github.json", "decisions/d1.json", "decisions_consent.json", "config.local.json"):
            self.assertNotIn(never, files, never)
        reasons = manifest["excluded_reasons"]
        self.assertEqual(reasons[".local_secret"], "managed")
        self.assertEqual(reasons[".kiro/agents"], "managed")            # rendered by the box each generation
        self.assertEqual(reasons["trust/sel_hmac.key"], "secret")
        self.assertEqual(reasons["gateway.log"], "runtime")
        self.assertEqual(reasons["dashboard-5476.sock"], "runtime")
        self.assertEqual(reasons[".kiro/crew-auth-staging"], "runtime")   # *-staging is excluded by name
        self.assertEqual(reasons[".kiro/unknown-child"], "unclassified")
        self.assertEqual(reasons[".kiro/settings"], "unclassified")
        # Owner-ish entries the gateway does not accept yet are reported, not archived and not fatal.
        for reported in ("connections", "decisions", "decisions_consent.json", "config.local.json", "mcp", "apps"):
            self.assertEqual(reasons[reported], "unclassified", reported)
        # Every kept path passes the gateway's own inventory rules (parity, see module docstring).
        for kept in files:
            self.assertTrue(gateway_valid_path(kept), kept)
        self.assertEqual(sorted(reasons), manifest["excluded"])
        # Deterministic: a second capture of the same home yields the same archive digest.
        manifest_again = capture(self.home, self.archive, self.manifest, quiesced=True, quiescence=threading.Lock())
        self.assertEqual(manifest_again["archive_sha256"], manifest["archive_sha256"])

    def test_excludes_dashboard_signing_key_link_without_reading_its_target(self):
        key = self.root / "auth-scratch" / "token_signing.key"
        key.parent.mkdir()
        key.write_bytes(b"fictional-managed-signing-secret")
        managed_link = self.home / "token_signing.key"
        managed_link.symlink_to(key)
        original_read = Path.read_bytes

        def read_public_file(path):
            self.assertNotIn(path, (managed_link, key))
            return original_read(path)

        with patch.object(Path, "read_bytes", read_public_file):
            manifest, archive, _, _ = self.captured()
        self.assertIn("token_signing.key", manifest["excluded"])
        self.assertNotIn("token_signing.key", manifest["files"])
        self.assertNotIn(b"fictional-managed-signing-secret", archive)
        key.unlink()
        self.captured()
        (self.home / "workspace" / "token_signing.key").symlink_to(key)
        key.write_bytes(b"fictional-managed-signing-secret")
        with patch.object(Path, "read_bytes", read_public_file):
            manifest, archive, _, _ = self.captured()  # skipped and named, never followed (2026-10-26)
        self.assertEqual(manifest["excluded_reasons"]["workspace/token_signing.key"], "runtime")
        self.assertNotIn(b"fictional-managed-signing-secret", archive)

    def test_fetches_exact_versions_and_rejects_wrong_or_incomplete_receipts(self):
        _, archive, manifest, hashes = self.captured()
        receipt = {label: {"bucket": "fixture", "key": label, "version_id": "first",
                           "sha256": hashes[label + "_sha256"]} for label in ("archive", "manifest")}
        objects = {("fixture", "archive", "first"): archive, ("fixture", "manifest", "first"): manifest,
                   ("fixture", "archive", "latest"): b"not our checkpoint"}
        requested = []

        def fetch(bucket, key, version):
            requested.append((bucket, key, version))
            return objects[(bucket, key, version)]

        restored = self.root / "recovered"
        restore_exact(restored, receipt, fetch)
        self.assertEqual(requested, [("fixture", "archive", "first"), ("fixture", "manifest", "first")])
        self.assertEqual((restored / "members/analyst/briefing.md").read_bytes(), self.fixture["members/analyst/briefing.md"])
        receipt["archive"]["version_id"] = "latest"
        with self.assertRaisesRegex(InvalidCheckpoint, "checksum"):
            restore_exact(self.root / "recovered-again", receipt, fetch)
        self.assertFalse((self.root / "recovered-again").exists())
        receipt["archive"]["version_id"] = ""
        with self.assertRaisesRegex(InvalidCheckpoint, "receipt"):
            restore_exact(self.root / "recovered-again", receipt, fetch)

    def test_excludes_managed_credential_directories(self):
        credential = self.home / "workspace" / ".aws" / "credentials"
        credential.parent.mkdir()
        credential.write_text("managed-secret")
        manifest, archive, _, _ = self.captured()
        self.assertIn("workspace/.aws", manifest["excluded"])
        self.assertNotIn(b"managed-secret", archive)

    def test_excludes_shell_credentials_inside_allowed_trees(self):
        names = ("workspace/.config/gh/hosts.yml", "sessions/.codex/auth.json",
                 "artifacts/.git-credentials", ".kiro/sessions/cli/.adp/token",
                 ".claude/.credentials.json", ".codex/auth.json", ".claude/model.env", ".codex/model.env")
        for index, name in enumerate(names):
            destination = self.home / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(f"shell-credential-fixture-{index}")
        manifest, archive, _, _ = self.captured()
        for index, name in enumerate(names):
            self.assertNotIn(name, manifest["files"])
            self.assertNotIn(f"shell-credential-fixture-{index}".encode(), archive)

    def test_shell_credentials_excluded(self):
        copied = (".claude/adp-dev-box-shell/key", ".codex/adp-dev-box-shell/config.json",
                  "workspace/adp-dev-box-shell/other-name.json")
        for name in copied:
            path = self.home / name
            path.parent.mkdir(parents=True)
            path.write_text("synthetic-runtime-secret")
        manifest, archive, _, _ = self.captured()
        for name in copied:
            parent = name.rsplit("/", 1)[0]
            self.assertEqual(manifest["excluded_reasons"][parent], "secret")
            self.assertNotIn(name, manifest["files"])
            self.assertFalse(gateway_valid_path(name))
        self.assertNotIn(b"synthetic-runtime-secret", archive)

    def test_claude_code_and_codex_state_is_archived_without_their_credentials(self):
        # #8558: the owner asked for Claude Code and Codex state in the backups; only credential files stay out.
        for name in (".claude/settings.json", ".claude/projects/-mnt-crew-workspace/turn.jsonl", ".codex/config.toml",
                     ".codex/sessions/s1.jsonl"):
            (self.home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / name).write_text("owner-state")
        (self.home / ".claude" / ".credentials.json").write_text("oauth-secret-fixture")
        (self.home / ".codex" / "auth.json").write_text("codex-secret-fixture")
        (self.home / ".claude.json").write_text("rendered-shim-fixture")
        (self.home / ".claude" / "model.env").write_text("ANTHROPIC_API_KEY=synthetic-secret")
        (self.home / ".codex" / "model.env").write_text("OPENAI_API_KEY=synthetic-secret")
        manifest, archive, _, _ = self.captured()
        for kept in (".claude/settings.json", ".claude/projects/-mnt-crew-workspace/turn.jsonl", ".codex/config.toml",
                     ".codex/sessions/s1.jsonl"):
            self.assertIn(kept, manifest["files"], kept)
        for never in (".claude/.credentials.json", ".codex/auth.json", ".claude.json",
                      ".claude/model.env", ".codex/model.env"):
            self.assertNotIn(never, manifest["files"], never)
        for secret in (b"oauth-secret-fixture", b"codex-secret-fixture", b"rendered-shim-fixture",
                       b"ANTHROPIC_API_KEY=synthetic-secret", b"OPENAI_API_KEY=synthetic-secret"):
            self.assertNotIn(secret, archive)
        self.assertEqual(manifest["excluded_reasons"][".claude/.credentials.json"], "secret")

    def test_final_capture_requires_writer_pause_and_background_sqlite_stays_valid(self):
        with self.assertRaisesRegex(InvalidCheckpoint, "writer quiescence"):
            capture(self.home, self.archive, self.manifest, quiesced=True)
        self.assertFalse(self.archive.exists())
        lock = threading.Lock()
        ready = threading.Event()
        stop = threading.Event()
        writes = [0]

        def writer():
            with closing(sqlite3.connect(self.home / "memory.db")) as database:
                database.execute("PRAGMA journal_mode=WAL")
                while not stop.is_set():
                    with lock:
                        database.execute("INSERT INTO memory VALUES (?, 'while-active')", (writes[0] + 3,))
                        database.commit()
                        writes[0] += 1
                        ready.set()
                    time.sleep(0.001)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            background = capture(self.home, self.archive, self.manifest)
            self.assertFalse(background["quiesced"])
            self.assertNotIn("memory.db-wal", background["files"])
            with tarfile.open(self.archive) as archive:
                database_bytes = archive.extractfile("memory.db").read()
            background_db = self.root / "background.db"
            background_db.write_bytes(database_bytes)
            with closing(sqlite3.connect(background_db)) as database:
                self.assertEqual(database.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertGreaterEqual(database.execute("SELECT COUNT(*) FROM memory").fetchone()[0], 2)

            captured_writes = [None]

            @contextmanager
            def quiescence():
                with lock:
                    captured_writes[0] = writes[0]
                    yield
                    self.assertEqual(writes[0], captured_writes[0])

            final = capture(self.home, self.archive, self.manifest,
                            quiesced=True, quiescence=quiescence())
            self.assertTrue(final["quiesced"])
            with tarfile.open(self.archive) as archive:
                final_db_bytes = archive.extractfile("memory.db").read()
            final_db = self.root / "final.db"
            final_db.write_bytes(final_db_bytes)
            with closing(sqlite3.connect(final_db)) as database:
                self.assertEqual(database.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(database.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
                                 captured_writes[0] + 1)
        finally:
            stop.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_online_sqlite_backup_contains_committed_wal_rows(self):
        database = self.home / "memory.db"
        with closing(sqlite3.connect(database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO memory VALUES (3, 'later')")
            writer.commit()
            manifest, _, _, _ = self.captured()
            self.assertIn("memory.db", manifest["files"])
        restored = self.root / "new-home"
        _, archive, manifest_bytes, receipt = self.captured()
        restore(restored, archive, manifest_bytes, **receipt)
        with closing(sqlite3.connect(restored / "memory.db")) as reader:
            self.assertEqual(reader.execute("SELECT * FROM memory ORDER BY revision").fetchall(),
                             [(2, "explicit"), (3, "later")])

    def test_corrupt_archive_does_not_create_restored_home(self):
        _, archive, manifest_bytes, receipt = self.captured()
        destination = self.root / "restore"
        with self.assertRaisesRegex(InvalidCheckpoint, "checksum"):
            restore(destination, archive + b"corruption", manifest_bytes, **receipt)
        self.assertFalse(destination.exists())

    def test_incompatible_crew_version_rejects_without_creating_home(self):
        import json

        _, archive, manifest_bytes, _ = self.captured()
        manifest = json.loads(manifest_bytes)
        manifest["crew_version"] = "0.7.2"
        incompatible = json.dumps(manifest).encode()
        destination = self.root / "incompatible-restore"
        with self.assertRaisesRegex(InvalidCheckpoint, "incompatible"):
            restore(destination, archive, incompatible, archive_sha256=hashlib.sha256(archive).hexdigest(),
                    manifest_sha256=hashlib.sha256(incompatible).hexdigest())
        self.assertFalse(destination.exists())

    def test_restore_empty_mount_rejects_divergence_and_failed_verification(self):
        _, archive, manifest, hashes = self.captured()
        receipt = {label: {"bucket": "fixture", "key": label, "version_id": "v1",
                           "sha256": hashes[label + "_sha256"]} for label in ("archive", "manifest")}
        objects = {"archive": archive, "manifest": manifest}
        mount = self.root / "mounted-volume"
        mount.mkdir()
        (mount / "newer-local-work").write_text("not saved")
        with self.assertRaisesRegex(InvalidCheckpoint, "empty mounted home"):
            restore_empty_mount(mount, receipt, lambda _, key, version: objects[key])
        self.assertEqual((mount / "newer-local-work").read_text(), "not saved")
        (mount / "newer-local-work").unlink()
        with self.assertRaisesRegex(InvalidCheckpoint, "checksum"):
            restore_empty_mount(mount, receipt, lambda _, key, version: b"bad" if key == "archive" else objects[key])
        self.assertEqual(list(mount.iterdir()), [])
        restore_empty_mount(mount, receipt, lambda _, key, version: objects[key])
        self.assertEqual((mount / "workspace/memory/preferences.md").read_bytes(), self.fixture["workspace/memory/preferences.md"])
        self.assertFalse((mount / "config.json").exists())
        with self.assertRaisesRegex(InvalidCheckpoint, "empty mounted home"):
            restore_empty_mount(mount, receipt, lambda _, key, version: objects[key])


    def test_restore_empty_mount_tolerates_the_hosts_state_directory(self):
        """A fresh retained disk already carries the host's root-only .adp-host state (SSH host keys, the
        owner's home) before the first checkpoint restore (dev 2026-10-26); it is not Crew content."""
        import tempfile
        from pathlib import Path as _Path
        with tempfile.TemporaryDirectory() as directory:
            mount = _Path(directory) / "home"
            mount.mkdir()
            (mount / crew_checkpoint.HOST_STATE_DIRECTORY / "ssh-host-keys").mkdir(parents=True)
            with self.assertRaises(Exception) as refused:  # the synthetic receipt fails inside the restore itself
                crew_checkpoint.restore_empty_mount(mount, {"id": "x"}, lambda *_a, **_k: None)
            self.assertNotIn("empty mounted home", str(refused.exception), "the occupancy test must not be what refuses")
            (mount / "memory.db").write_bytes(b"content")
            with self.assertRaises(crew_checkpoint.InvalidCheckpoint) as occupied:
                crew_checkpoint.restore_empty_mount(mount, {"id": "x"}, lambda *_a, **_k: None)
            self.assertIn("empty mounted home", str(occupied.exception))

    def test_failed_activation_leaves_partial_volume_visible_for_manual_recovery(self):
        _, archive, manifest, hashes = self.captured()
        receipt = {label: {"bucket": "fixture", "key": label, "version_id": "v1",
                           "sha256": hashes[label + "_sha256"]} for label in ("archive", "manifest")}
        objects = {"archive": archive, "manifest": manifest}
        mount = self.root / "interrupted-volume"
        mount.mkdir()
        from crew_checkpoint import os as checkpoint_os
        original_rename = checkpoint_os.rename
        moves = [0]

        def interrupted_rename(source, destination):
            if Path(source).parent.name == ".crew-verified-restore":
                moves[0] += 1
                if moves[0] == 2:
                    raise OSError("volume write interrupted")
            return original_rename(source, destination)

        with patch.object(checkpoint_os, "rename", side_effect=interrupted_rename):
            with self.assertRaisesRegex(OSError, "volume write interrupted"):
                restore_empty_mount(mount, receipt, lambda _, key, version: objects[key])
        self.assertEqual(moves[0], 2)
        self.assertTrue((mount / ".crew-verified-restore").exists())
        with self.assertRaisesRegex(InvalidCheckpoint, "empty mounted home"):
            restore_empty_mount(mount, receipt, lambda _, key, version: objects[key])

    def test_reject_archive_path_even_with_matching_digests(self):
        _, _, manifest_bytes, _ = self.captured()
        archive_file = io.BytesIO()
        with tarfile.open(fileobj=archive_file, mode="w") as tar:
            member = tarfile.TarInfo("../escape")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
        malicious = archive_file.getvalue()
        import json
        manifest = json.loads(manifest_bytes)
        manifest["archive_sha256"] = hashlib.sha256(malicious).hexdigest()
        manifest_bytes = json.dumps(manifest).encode()
        with self.assertRaises(InvalidCheckpoint):
            restore(self.root / "restore", malicious, manifest_bytes,
                    archive_sha256=hashlib.sha256(malicious).hexdigest(),
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())
        self.assertFalse((self.root / "restore").exists())


    def test_owner_codex_snapshot_keeps_small_metadata_without_rollout_payloads(self):
        owner = self.root / "owner"
        codex = owner / ".codex"
        codex.mkdir(parents=True, mode=0o700)
        codex.chmod(0o700)
        (codex / "history.jsonl").write_text('{"session_id":"session-one","text":"hello"}\n')
        (codex / "session_index.jsonl").write_text('{"id":"session-one","title":"hello"}\n')
        (codex / "config.toml").write_bytes(crew_checkpoint.OWNER_CODEX_CONFIG)
        shell = (Path(__file__).resolve().parents[3] / "user-services" / "dev-box" / "image" / "prepare_shell.sh").read_text()
        image_config = shell.split("cat > /home/ec2-user/.codex/config.toml <<'CONFIG'\n", 1)[1].split("\nCONFIG\n", 1)[0]
        self.assertEqual(crew_checkpoint.OWNER_CODEX_CONFIG, (image_config + "\n").encode())
        stale = self.home / ".codex" / "owner-shell"
        stale.mkdir(parents=True)
        (stale / "old.jsonl").write_text("prior generation")
        sessions = codex / "sessions" / "2026" / "10" / "23"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.jsonl"
        header = {"type": "session_meta", "payload": {"id": "11111111-2222-3333-4444-555555555555",
                  "timestamp": "2026-10-23T00:00:00Z", "cwd": "/home/ec2-user", "instructions": "private-fixture"}}
        with rollout.open("wb") as writer:
            writer.write(json.dumps(header).encode() + b"\n")
            writer.truncate(200 * 1024 * 1024)
        self.assertEqual(rollout.stat().st_size, 200 * 1024 * 1024)
        (codex / "auth.json").write_text("private-auth-fixture")
        (codex / "model.env").write_text("private-model-fixture")
        manifest = capture(self.home, self.archive, self.manifest, owner_home=owner)
        prefix = crew_checkpoint.OWNER_CODEX_PREFIX
        files = manifest["files"]
        for name in ("config.toml", "history.jsonl", "session_index.jsonl"):
            self.assertIn(f"{prefix}/{name}", files)
        session_meta = f"{prefix}/sessions/2026/10/23/metadata-2026-10-23T00-00-00-11111111-2222-3333-4444-555555555555.json"
        self.assertIn(session_meta, files)
        self.assertNotIn(f"{prefix}/old.jsonl", files)
        self.assertEqual(manifest["excluded_reasons"][prefix], "managed")
        self.assertEqual(files[session_meta]["size"], len(json.dumps({"cwd": "/home/ec2-user", "id": header["payload"]["id"],
                                                                      "timestamp": "2026-10-23T00:00:00Z"}, sort_keys=True).encode()))
        self.assertNotIn(f"{prefix}/sessions/2026/10/23/{rollout.name}", files)
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/sessions/2026/10/23/{rollout.name}"], "runtime")
        for path in files:
            self.assertTrue(gateway_valid_path(path), path)
        self.assertLess(self.archive.stat().st_size, 512 * 1024 * 1024)
        self.assertLess(self.archive.stat().st_size, 8 * 1024 * 1024)
        archive = self.archive.read_bytes()
        self.assertNotIn(b"private-fixture", archive)
        self.assertNotIn(b"private-auth-fixture", archive)
        self.assertNotIn(b"private-model-fixture", archive)
        with self.assertRaisesRegex(InvalidCheckpoint, "excluded Codex rollout"):
            crew_checkpoint.safe_name(f"{prefix}/sessions/2026/10/23/{rollout.name}")

    def test_owner_claude_snapshot_extracts_text_turns_only(self):
        owner = self.root / "owner"
        project = owner / ".claude" / "projects" / "-home-ec2-user"
        project.mkdir(parents=True, mode=0o700)
        owner.chmod(0o700)
        session = "2b4f8a9c-1d2e-4f3a-8b7c-0a1b2c3d4e5f"
        records = [
            {"type": "user", "isMeta": True, "timestamp": "2026-10-27T08:00:00.000Z", "message": {"role": "user", "content": "private-meta-fixture"}},
            {"type": "permission-mode", "permissionMode": "default", "sessionId": session},
            {"type": "user", "timestamp": "2026-10-27T08:00:01.000Z", "message": {"role": "user", "content":
                "<system-reminder>private-reminder-fixture</system-reminder>fix the login bug, token ghp_" + "A" * 24 + " leaked"}},
            {"type": "assistant", "timestamp": "2026-10-27T08:00:02.000Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "private-thinking-fixture"}]}},
            {"type": "assistant", "timestamp": "2026-10-27T08:00:03.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Looking."}]}},
            {"type": "assistant", "timestamp": "2026-10-27T08:00:04.000Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "private-tool-fixture"}}]}},
            {"type": "user", "timestamp": "2026-10-27T08:00:05.000Z", "toolUseResult": {"x": 1}, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "private-result-fixture"}]}},
            {"type": "assistant", "timestamp": "2026-10-27T08:00:06.000Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Fixed it. " + "x" * 5000}]}},
            {"type": "user", "isSidechain": True, "timestamp": "2026-10-27T08:00:07.000Z", "message": {"role": "user", "content": "private-sidechain-fixture"}},
        ]
        (project / f"{session}.jsonl").write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
        (project / session).mkdir()
        (project / session / "subagents").mkdir()
        (project / session / "subagents" / "agent-1.jsonl").write_text(json.dumps(records[2]) + "\n")
        (project / "linked.jsonl").symlink_to(project / f"{session}.jsonl")
        (project / "empty.jsonl").write_text(json.dumps({"type": "summary", "summary": "private-summary-fixture"}) + "\n")
        huge = project / "11111111-2222-3333-4444-555555555555.jsonl"
        with huge.open("wb") as writer:
            writer.write(json.dumps(records[2]).encode() + b"\n")
            writer.write(b"{" + b"x" * (2 * 1024 * 1024) + b"\n")  # one over-long record is skipped, not loaded
            writer.write(json.dumps(records[4]).encode() + b"\n")
            writer.truncate(6 * 1024 * 1024)
        stale = self.home / ".claude" / "owner-shell"
        stale.mkdir(parents=True)
        (stale / "old.jsonl").write_text("prior generation")
        with unittest.mock.patch.object(crew_checkpoint, "OWNER_CLAUDE_READ_LIMIT", 3 * 1024 * 1024), \
                unittest.mock.patch.object(crew_checkpoint, "OWNER_CLAUDE_LINE_LIMIT", 1024 * 1024):
            manifest = capture(self.home, self.archive, self.manifest, owner_home=owner)
        prefix = f"{crew_checkpoint.OWNER_CLAUDE_PREFIX}/projects/-home-ec2-user"
        files = manifest["files"]
        self.assertIn(f"{prefix}/{session}.jsonl", files)
        self.assertNotIn(f"{prefix}/{session}/subagents/agent-1.jsonl", files)
        self.assertNotIn(f"{prefix}/linked.jsonl", files)
        self.assertNotIn(f"{prefix}/empty.jsonl", files)
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/empty.jsonl"], "runtime")
        self.assertNotIn(f"{crew_checkpoint.OWNER_CLAUDE_PREFIX}/old.jsonl", files)
        self.assertEqual(manifest["excluded_reasons"][crew_checkpoint.OWNER_CLAUDE_PREFIX], "managed")
        for path in files:
            self.assertTrue(gateway_valid_path(path), path)
        with tarfile.open(self.archive) as archive:
            extract = [json.loads(line) for line in archive.extractfile(f"{prefix}/{session}.jsonl").read().splitlines()]
            truncated = [json.loads(line) for line in archive.extractfile(f"{prefix}/{huge.name}").read().splitlines()]
        self.assertEqual(extract[0], {"type": "ai-title", "aiTitle": "fix the login bug, token [redacted] leaked", "sessionId": session,
                                      "extract": "adp-owner-shell", "truncated": False})
        self.assertEqual([(r["type"], r["timestamp"]) for r in extract[1:]],
                         [("user", "2026-10-27T08:00:01.000Z"), ("assistant", "2026-10-27T08:00:03.000Z"), ("assistant", "2026-10-27T08:00:06.000Z")])
        self.assertEqual(extract[1]["message"]["content"], [{"type": "text", "text": "fix the login bug, token [redacted] leaked"}])
        self.assertEqual(extract[2]["message"]["content"], [{"type": "text", "text": "Looking."}, {"type": "tool_use"}])
        self.assertEqual(extract[3]["message"]["content"][1], {"type": "tool_use"})
        self.assertEqual(len(extract[3]["message"]["content"][0]["text"]), crew_checkpoint.OWNER_CLAUDE_TURN_CHARS + 1)
        self.assertTrue(truncated[0]["truncated"])
        self.assertEqual([r["type"] for r in truncated[1:]], ["user", "assistant"])
        archive = self.archive.read_bytes()
        for secret in (b"private-meta-fixture", b"private-reminder-fixture", b"private-thinking-fixture", b"private-tool-fixture",
                       b"private-result-fixture", b"private-sidechain-fixture", b"private-summary-fixture", b"ghp_AAAA", b"prior generation"):
            self.assertNotIn(secret, archive, secret)
        self.assertLess(self.archive.stat().st_size, 1024 * 1024)

    def test_owner_claude_snapshot_budget_keeps_newest_sessions_and_skips_foreign_files(self):
        owner = self.root / "owner"
        project = owner / ".claude" / "projects" / "-workspace"
        project.mkdir(parents=True, mode=0o700)
        owner.chmod(0o700)
        for index, stamp in enumerate(("2026-10-25T08:00:00.000Z", "2026-10-26T08:00:00.000Z", "2026-10-27T08:00:00.000Z")):
            path = project / f"0000000{index}-0000-4000-8000-000000000000.jsonl"
            path.write_text(json.dumps({"type": "user", "timestamp": stamp, "message": {"role": "user", "content": f"prompt {index} " + "y" * 300}}) + "\n")
            os.utime(path, (1792000000 + index, 1792000000 + index))
        other = project / "00000009-0000-4000-8000-000000000000.jsonl"
        other.write_text(json.dumps({"type": "user", "timestamp": "2026-10-27T09:00:00.000Z", "message": {"role": "user", "content": "private-hardlink-fixture"}}) + "\n")
        os.link(other, project / "hardlinked.jsonl")
        with unittest.mock.patch.object(crew_checkpoint, "OWNER_CLAUDE_TOTAL_LIMIT", 1500):
            manifest = capture(self.home, self.archive, self.manifest, owner_home=owner)
        prefix = f"{crew_checkpoint.OWNER_CLAUDE_PREFIX}/projects/-workspace"
        kept = sorted(name for name in manifest["files"] if name.startswith(prefix))
        self.assertEqual(kept, [f"{prefix}/00000001-0000-4000-8000-000000000000.jsonl", f"{prefix}/00000002-0000-4000-8000-000000000000.jsonl"])
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/00000000-0000-4000-8000-000000000000.jsonl"], "runtime")
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/hardlinked.jsonl"], "runtime")
        self.assertNotIn(b"private-hardlink-fixture", self.archive.read_bytes())

    def test_owner_codex_snapshot_extracts_rollout_text_turns_without_tool_payloads(self):
        owner = self.root / "owner"
        codex = owner / ".codex"
        sessions = codex / "sessions" / "2026" / "10" / "27"
        sessions.mkdir(parents=True, mode=0o700)
        owner.chmod(0o700)
        codex.chmod(0o700)
        (codex / "config.toml").write_bytes(crew_checkpoint.OWNER_CODEX_CONFIG)
        session = "01a17b85-171a-7e31-8e39-a991b5f0ca9b"
        (codex / "history.jsonl").write_text(json.dumps({"session_id": session, "ts": 1792000000, "text": "rank repos by commits"}) + "\n")
        rollout = sessions / f"rollout-2026-10-27T04-01-22-{session}.jsonl"
        records = [
            {"type": "session_meta", "timestamp": "2026-10-27T04:01:22.000Z", "payload": {"id": session, "timestamp": "2026-10-27T04:01:22Z",
                                                                                             "cwd": "/home/ec2-user",
                                                                                             # dev 2026-10-27: Codex's header is ~22 KB because of the base instructions
                                                                                             "base_instructions": "private-instructions-fixture " + "i" * 24000}},
            {"type": "response_item", "timestamp": "2026-10-27T04:01:23.000Z", "payload": {"type": "message", "role": "developer",
                                                                                              "content": [{"type": "input_text", "text": "private-developer-fixture"}]}},
            {"type": "response_item", "timestamp": "2026-10-27T04:01:24.000Z", "payload": {"type": "message", "role": "user",
                                                                                              "content": [{"type": "input_text", "text": "<environment_context>private-env-fixture</environment_context>"},
                                                                                                          {"type": "input_text", "text": "rank repos by commits"}]}},
            {"type": "response_item", "timestamp": "2026-10-27T04:01:25.000Z", "payload": {"type": "function_call", "name": "shell", "arguments": "private-call-fixture"}},
            {"type": "response_item", "timestamp": "2026-10-27T04:01:26.000Z", "payload": {"type": "function_call_output", "output": "private-output-fixture"}},
            {"type": "response_item", "timestamp": "2026-10-27T04:01:27.000Z", "payload": {"type": "message", "role": "assistant", "status": "completed",
                                                                                              "content": [{"type": "output_text", "text": "Here is the ranking."}]}},
            {"type": "event_msg", "timestamp": "2026-10-27T04:01:28.000Z", "payload": {"type": "task_complete", "last_agent_message": "private-event-fixture"}},
        ]
        rollout.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
        manifest = capture(self.home, self.archive, self.manifest, owner_home=owner)
        prefix = f"{crew_checkpoint.OWNER_CODEX_PREFIX}/sessions/2026/10/27"
        turns_path = f"{prefix}/turns-2026-10-27T04-01-22-{session}.jsonl"
        self.assertIn(turns_path, manifest["files"])
        self.assertIn(f"{prefix}/metadata-2026-10-27T04-01-22-{session}.json", manifest["files"])
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/{rollout.name}"], "runtime")
        self.assertTrue(gateway_valid_path(turns_path))
        with tarfile.open(self.archive) as archive:
            extract = [json.loads(line) for line in archive.extractfile(turns_path).read().splitlines()]
        self.assertEqual(extract[0], {"session_id": session, "extract": "adp-owner-shell", "truncated": False})
        self.assertEqual(extract[1:], [
            {"role": "user", "ts": "2026-10-27T04:01:24.000Z", "text": "rank repos by commits"},
            {"role": "assistant", "ts": "2026-10-27T04:01:27.000Z", "text": "Here is the ranking."},
        ])
        archive_bytes = self.archive.read_bytes()
        for secret in (b"private-instructions-fixture", b"private-developer-fixture", b"private-env-fixture", b"private-call-fixture",
                       b"private-output-fixture", b"private-event-fixture"):
            self.assertNotIn(secret, archive_bytes, secret)

    def test_owner_codex_snapshot_ignores_secret_config_and_symlinked_history(self):
        owner = self.root / "owner"
        codex = owner / ".codex"
        codex.mkdir(parents=True, mode=0o700)
        codex.chmod(0o700)
        (codex / "config.toml").write_text('api_key = "private-config-fixture"\n')
        (codex / "history.jsonl").symlink_to(codex / "config.toml")
        manifest = capture(self.home, self.archive, self.manifest, owner_home=owner)
        prefix = crew_checkpoint.OWNER_CODEX_PREFIX
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/config.toml"], "secret")
        self.assertEqual(manifest["excluded_reasons"][f"{prefix}/history.jsonl"], "secret")
        self.assertNotIn(b"private-config-fixture", self.archive.read_bytes())


if __name__ == "__main__":
    unittest.main()


class GatewayParity(unittest.TestCase):
    def test_host_inventory_mirrors_the_gateway_rules(self):
        # If the gateway refuses a path this helper keeps, the checkpoint is never verified
        # ("unsafe archive inventory"); the host must therefore keep at most what the gateway allows.
        rules = gateway_inventory_rules()
        self.assertEqual(crew_checkpoint.FILES, rules["ALLOWED_FILES"])
        self.assertEqual(crew_checkpoint.TREES, rules["ALLOWED_TREES"])
        self.assertTrue(rules["EXCLUDED_NAMES"] <= crew_checkpoint.EXCLUDED_NAMES)
        self.assertTrue(set(rules["EXCLUDED_SUFFIXES"]) <= set(crew_checkpoint.EXCLUDED_SUFFIXES))
        self.assertEqual(crew_checkpoint.KIRO_TREES, frozenset({("sessions", "cli")}))
