import hashlib
import json
import os
from pathlib import Path
from unittest import mock
import unittest

import cloud_sync
from cloud_sync import canonical, destination, post_batch, projection, sync
from install_background import definition
import test_topics as fixtures


class CloudSyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkspaceTests()
        self.fixture.setUp()
        self.workspace = self.fixture.workspace
        self.workspace.sync()
        self.cache = self.fixture.root / "cloud"
        self.config = {"endpoint": "https://brain.example.com/api/chat-library/sync",
                       "expected_host": "brain.example.com", "collector_id": "test-mac",
                       "project_ref": "abcdefghijklmnopqrst"}

    def tearDown(self):
        self.fixture.tearDown()

    def test_projection_visible_only_and_source_unchanged(self):
        before = hashlib.sha256(self.fixture.history.read_bytes()).hexdigest()
        with self.workspace.source() as db:
            original = db.execute("SELECT count(*) FROM messages").fetchone()[0]
        rows = list(projection(self.workspace))
        messages = [message for row in rows if row["kind"] == "chunk"
                    for message in json.loads(row["payload_json"])["messages"]]
        self.assertEqual(len(messages), original)
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in messages))
        self.assertEqual(before, hashlib.sha256(self.fixture.history.read_bytes()).hexdigest())
        self.assertNotIn(str(self.fixture.root), canonical(rows))
        self.assertFalse(json.loads(rows[-1]["payload_json"])["all_ai_complete"])

    def test_tool_text_excluded_credentials_redacted_and_chunks_precede_header(self):
        import sqlite3
        with sqlite3.connect(self.fixture.history) as db:
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                       ("secret", "mixed", 3, "assistant", "sk-" + "s" * 30, 5))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                       ("tool", "mixed", 4, "tool", "TOOL-PRIVATE-TEXT", 6))
        self.workspace.sync()
        rows = list(projection(self.workspace))
        serialized = canonical(rows)
        self.assertNotIn("sk-" + "s" * 30, serialized)
        self.assertNotIn("TOOL-PRIVATE-TEXT", serialized)
        self.assertIn("[credential redacted]", serialized)
        index = {row["record_id"]: i for i, row in enumerate(rows)}
        for row in rows:
            if row["kind"] == "conversation":
                for chunk in json.loads(row["payload_json"])["chunks"]:
                    self.assertLess(index[chunk], index[row["record_id"]])

    def test_dry_run_and_repeat_do_not_duplicate_uploads(self):
        sent = []
        def send(config, batch):
            sent.extend(batch)
        with mock.patch("cloud_sync.now", return_value="2026-10-19T20:00:00+00:00"):
            preview = sync(self.workspace, self.config, self.cache, False, send)
            self.assertGreater(preview["changed"], 0)
            self.assertEqual(sent, [])
            self.assertFalse(self.cache.exists())
            first = sync(self.workspace, self.config, self.cache, True, send)
            count = len(sent)
            second = sync(self.workspace, self.config, self.cache, True, send)
        self.assertEqual(first["uploaded_and_verified"], count)
        self.assertEqual((second["changed"], second["uploaded_and_verified"], len(sent)), (0, 0, count))

    def test_failed_batch_is_not_acknowledged_and_retry_retains_progress(self):
        sent = []
        def fail_second(config, batch):
            if sent:
                raise ValueError("offline")
            sent.extend(batch)
        with mock.patch("cloud_sync.MAX_BATCH", 1500):
            with self.assertRaises(ValueError):
                sync(self.workspace, self.config, self.cache, True, fail_second)
        acknowledged = json.loads((self.cache / "cloud-checkpoint.json").read_text())["hashes"]
        retry = []
        sync(self.workspace, self.config, self.cache, True, lambda config, batch: retry.extend(batch))
        self.assertTrue(acknowledged)
        self.assertFalse(set(acknowledged) & {r["record_id"] for r in retry})

    def test_destination_and_receipt_must_match(self):
        for endpoint in ("http://brain.example.com/api/chat-library/sync",
                         "https://other.example.com/api/chat-library/sync",
                         "https://brain.example.com/api/chat-library/sync?token=secret"):
            with self.assertRaises(ValueError):
                destination(dict(self.config, endpoint=endpoint))
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return canonical({"project_ref": "wrong", "receipts": {}}).encode()
        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.dict(os.environ, {"HSTRY_BRAIN_SYNC_TOKEN": "t" * 40}):
            with self.assertRaises(ValueError):
                post_batch(self.config, [next(projection(self.workspace))], opener)
        self.assertIsNone(cloud_sync.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.com"))

    def test_checkpoint_is_bound_to_collector_and_project(self):
        sync(self.workspace, self.config, self.cache, True, lambda config, batch: None)
        with self.assertRaises(ValueError):
            sync(self.workspace, dict(self.config, collector_id="other-mac"), self.cache, True)

    def test_background_dry_run_never_starts_collection(self):
        import background
        config = self.fixture.root / "background.json"
        config.write_text(json.dumps({"history": str(self.fixture.history), "state": str(self.fixture.state)}))
        with mock.patch("background.configured_refresh") as collect:
            result = background.run(config, False)
        collect.assert_not_called()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["all_ai_complete"])

    def test_background_failure_retains_private_redacted_diagnostics(self):
        import background
        import sqlite3
        config = self.fixture.root / "background.json"
        config.write_text(json.dumps({"history": str(self.fixture.history), "state": str(self.fixture.state)}))
        before = self.fixture.source_hash()
        with mock.patch("background.configured_refresh", side_effect=sqlite3.DatabaseError("authorization denied sk-" + "s" * 30)):
            with self.assertRaises(RuntimeError):
                background.run(config, True)
        result = json.loads((self.fixture.state.parent / "background-status.json").read_text())
        self.assertEqual((result["status"], result["error_type"]), ("failed", "DatabaseError"))
        self.assertEqual(result["error"], "authorization denied [credential redacted]")
        self.assertEqual(before, self.fixture.source_hash())

    def test_launch_agent_runs_without_browser_or_secret_in_arguments(self):
        value = definition("/private/config.json", "/private/state", "/usr/bin/python3")
        self.assertEqual((value["StartInterval"], value["RunAtLoad"]), (300, True))
        self.assertIn("--apply", value["ProgramArguments"])
        self.assertNotIn("serve", value["ProgramArguments"])
        self.assertNotIn("TOKEN", canonical(value))



    def test_cowork_skips_known_sessions_but_detects_transcript_changes(self):
        from adapter_import import cowork_plan
        root = self.fixture.root / "cowork"
        meta = root / "account/org/local_one.json"
        meta.parent.mkdir(parents=True)
        meta.write_text("{}")
        transcript = meta.with_suffix("") / ".claude/projects/project/session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("first\n")
        previous = {"one": {"source_file": str(meta), "messages": []}}
        report = {"checked_at": "2099-01-01T00:00:00+00:00"}
        retained, selected, signatures, count = cowork_plan(root, previous, report, "cowork")
        self.assertEqual((retained, selected, count), (previous, [], 1))
        report["cowork_files"] = {"cowork": signatures}
        transcript.write_text("first\nsecond\n")
        self.assertEqual(cowork_plan(root, previous, report, "cowork")[1], [meta])
        unknown = meta.with_name("local_new.json")
        unknown.write_text("{}")
        os.utime(unknown, (1, 1))
        self.assertIn(unknown, cowork_plan(root, previous, report, "cowork")[1])

    def test_source_stat_cache_detects_same_timestamp_size_changes(self):
        from adapter_import import source_signature
        root = self.fixture.root / "provider"
        root.mkdir()
        path = root / "history.json"
        path.write_text("first")
        before = path.stat()
        first = source_signature(root)
        self.assertEqual(first, source_signature(root))
        path.write_text("longer content")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(first, source_signature(root))

    def test_import_lease_recovers_after_process_exit(self):
        import subprocess
        import sys
        from local_import import import_lease
        cache = self.fixture.root / "crash-lease"
        script = "import os,sys;sys.path.insert(0,sys.argv[1]);from local_import import import_lease\nwith import_lease(sys.argv[2]):os._exit(0)"
        subprocess.run([sys.executable, "-c", script, str(Path(cloud_sync.__file__).parent), str(cache)], check=True)
        with import_lease(cache):
            pass

    def test_dashboard_defers_imports_to_installed_background_owner(self):
        import background
        from local_import import configured_refresh
        path = self.fixture.state.parent / "local-import-config.json"
        path.write_text(json.dumps({"enabled": True, "history": str(self.fixture.history), "background_only": True}))
        with mock.patch("local_import.collect") as collect:
            self.assertEqual(configured_refresh(self.workspace)["managed_by"], "background collector")
        collect.assert_not_called()
        config = self.fixture.root / "background.json"
        config.write_text(json.dumps({"history": str(self.fixture.history), "state": str(self.fixture.state)}))
        with mock.patch("background.configured_refresh", return_value={"enabled": False}) as collect:
            self.assertEqual(background.run(config, True)["status"], "success")
        self.assertTrue(collect.call_args.kwargs["background"])


if __name__ == "__main__":
    unittest.main()
