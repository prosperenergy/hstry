import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from local_import import apply, collect, configured_refresh, digest_other_sources, import_lease, parse_session, private_json
from topics import Workspace
from web import make_server


class LocalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.path = self.sessions / "rollout.jsonl"
        self.rows = [
            {"type": "session_meta", "timestamp": "2026-10-01T10:00:00Z", "payload": {"id": "native-id", "thread_source": "user"}},
            {"type": "response_item", "timestamp": "2026-10-01T10:00:01Z", "payload": {"type": "message", "id": "bootstrap", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions <environment_context>"}]}},
            self.message("one", "user", "Battery project status"),
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "private tool output"}},
            self.message("two", "assistant", "Review the saved proposal"),
            self.message("hidden", "developer", "internal instructions"),
        ]
        self.write()
        self.db = self.root / "history.db"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
                CREATE TABLE sources(id TEXT PRIMARY KEY,adapter TEXT,path TEXT,config TEXT,last_sync_at INTEGER);
                CREATE TABLE conversations(id TEXT PRIMARY KEY,source_id TEXT,external_id TEXT,title TEXT,
                    created_at INTEGER,updated_at INTEGER,workspace TEXT,metadata TEXT,provider TEXT,harness TEXT,
                    version INTEGER DEFAULT 0,message_count INTEGER DEFAULT 0,UNIQUE(source_id,external_id));
                CREATE TABLE messages(id TEXT PRIMARY KEY,conversation_id TEXT,idx INTEGER,role TEXT,content TEXT,
                    created_at INTEGER,metadata TEXT,UNIQUE(conversation_id,idx));
                INSERT INTO sources VALUES('existing','original','source','{}',1);
                INSERT INTO conversations(id,source_id,external_id,title,created_at) VALUES('old','existing','old-id','Existing',1);
                INSERT INTO messages VALUES('old-message','old',0,'user','Unrelated evidence',1,'{}');
            """)

    def tearDown(self):
        self.temp.cleanup()

    def message(self, identity, role, content):
        return {"type": "response_item", "timestamp": "2026-10-01T10:00:02Z",
                "payload": {"type": "message", "id": identity, "role": role,
                            "content": [{"type": "text", "text": content}]}}

    def write(self):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.rows))

    def collect(self):
        return collect({"codex": [self.sessions]}, self.root / "cache")

    def test_visible_text_only_and_source_unchanged(self):
        raw = self.path.read_bytes()
        conv = parse_session(self.path, "codex")
        self.assertEqual([(m["role"], m["content"]) for m in conv["messages"]], [
            ("user", "Battery project status"), ("assistant", "Review the saved proposal")])
        self.assertEqual(self.path.read_bytes(), raw)

    def test_subagent_inherited_metadata_does_not_become_parent_chat(self):
        self.rows[0]["payload"].update(thread_source="subagent", source={"subagent": {}}, id="worker-id")
        self.rows.append({"type": "session_meta", "payload": {"id": "parent-id"}})
        self.write()
        self.assertIsNone(parse_session(self.path, "codex"))

    def test_partial_tail_waits_until_complete_record(self):
        with self.path.open("a") as stream:
            stream.write('{"type":')
        conv = parse_session(self.path, "codex")
        self.assertTrue(conv["partial_tail_pending"])
        self.assertEqual(len(conv["messages"]), 2)

    def test_cache_skips_unchanged_and_binds_roots(self):
        _, first = self.collect()
        _, second = self.collect()
        self.assertEqual((first["scanned"], second["scanned"], second["unchanged"]), (1, 0, 1))
        with self.assertRaisesRegex(ValueError, "different source roots"):
            collect({"codex": [self.root]}, self.root / "cache")

    def test_context_tags_do_not_become_display_titles(self):
        self.rows.insert(2, self.message("ambient", "user", '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'))
        self.write()
        conv = parse_session(self.path, "codex")
        self.assertEqual(conv["title"], "[Local codex] Battery project status")
        # The historical message remains intact; only the derived label changes.
        self.assertTrue(conv["messages"][0]["content"].startswith("<external_codex_apps_open_page>"))

    def test_display_name_refreshes_cached_chat_without_rewriting_messages(self):
        convs, report = self.collect()
        apply(self.db, convs, report, self.root / "backups", False)
        with sqlite3.connect(self.db) as db:
            before = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        native = self.root / "state_5.sqlite"
        with sqlite3.connect(native) as db:
            db.executescript("CREATE TABLE threads(id TEXT,title TEXT,name TEXT);")
            db.execute("INSERT INTO threads VALUES(?,?,?)", ("native-id", "Old title", "Chat Organizer"))
        native_bytes = native.read_bytes()
        convs, report = self.collect()
        self.assertEqual(report["unchanged"], 1)
        result = apply(self.db, convs, report, self.root / "backups", False)
        self.assertEqual((result["sessions_changed"], result["messages_added"]), (1, 0))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT title FROM conversations WHERE source_id!='existing'").fetchone(), ("[Local codex] Chat Organizer",))
            self.assertEqual(db.execute("SELECT * FROM messages ORDER BY id").fetchall(), before)
        self.assertEqual(native.read_bytes(), native_bytes)
        self.assertEqual(apply(self.db, convs, report, self.root / "backups", False)["sessions_changed"], 0)

    def test_dry_run_backup_append_idempotence_and_unrelated_sources(self):
        convs, report = self.collect()
        with sqlite3.connect(self.db) as db:
            before = digest_other_sources(db)
        raw = self.db.read_bytes()
        preview = apply(self.db, convs, report, self.root / "backups")
        self.assertEqual((preview["messages_added"], preview["dry_run"]), (2, True))
        self.assertEqual(self.db.read_bytes(), raw)
        first = apply(self.db, convs, report, self.root / "backups", False)
        self.assertTrue(Path(first["backup"]).is_file())
        self.assertTrue(first["unrelated_sources_unchanged"])
        with sqlite3.connect(self.db) as db:
            anchors = db.execute("SELECT id FROM messages ORDER BY id").fetchall()
            self.assertEqual(digest_other_sources(db), before)
        again = apply(self.db, convs, report, self.root / "backups", False)
        self.assertEqual(again["messages_added"], 0)
        self.rows.append(self.message("three", "user", "Append the next action"))
        self.write()
        convs, report = self.collect()
        appended = apply(self.db, convs, report, self.root / "backups", False)
        self.assertEqual(appended["messages_added"], 1)
        self.assertTrue(appended["daily_checkpoint_reused"])
        self.assertEqual([p.resolve() for p in (self.root / "backups").glob("*.sqlite")], [Path(first["backup"])])
        with sqlite3.connect(self.db) as db:
            self.assertTrue(set(anchors).issubset(set(db.execute("SELECT id FROM messages").fetchall())))

    def test_source_rewrite_rejected_without_losing_imported_evidence(self):
        convs, report = self.collect()
        apply(self.db, convs, report, self.root / "backups", False)
        raw = self.db.read_bytes()
        self.rows[2] = self.message("one", "user", "Changed historical message")
        self.write()
        convs, report = self.collect()
        with self.assertRaisesRegex(ValueError, "prefix changed"):
            apply(self.db, convs, report, self.root / "backups", False)
        self.assertEqual(self.db.read_bytes(), raw)

    def test_explicit_additional_source_preserves_existing_imports(self):
        convs, report = self.collect()
        apply(self.db, convs, report, self.root / "backups", False)
        with sqlite3.connect(self.db) as db:
            before = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        extra = {"cowork": convs["codex"]}
        report["roots"]["cowork"] = [str(self.sessions)]
        report["coverage"]["cowork"] = {"scope": "Explicit visible-text source copy"}
        result = apply(self.db, extra, report, self.root / "backups", False,
                       source_ids={"cowork": "cowork-visible"})
        self.assertEqual(result["messages_added"], 2)
        with sqlite3.connect(self.db) as db:
            self.assertTrue(set(before).issubset(set(db.execute("SELECT * FROM messages ORDER BY id").fetchall())))
        with self.assertRaisesRegex(ValueError, "distinct explicit source"):
            apply(self.db, extra, report, self.root / "backups", source_ids={"cowork": "same", "other": "same"})

    def test_wal_source_produces_a_standalone_reusable_checkpoint(self):
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA journal_mode=WAL")
            convs, report = self.collect()
            first = apply(self.db, convs, report, self.root / "backups", False)
            with sqlite3.connect(Path(first["backup"]).as_uri() + "?mode=ro", uri=True) as backup:
                self.assertEqual(backup.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.rows.append(self.message("wal-next", "user", "Append with WAL active"))
            self.write()
            convs, report = self.collect()
            self.assertTrue(apply(self.db, convs, report, self.root / "backups", False)["daily_checkpoint_reused"])

    def test_claude_excludes_tool_results_reasoning_and_sidechains(self):
        rows = [{"type": "user", "sessionId": "claude-id", "uuid": "c1", "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": "Find the battery plan"}},
                {"type": "user", "sessionId": "claude-id", "uuid": "c2", "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool secret"}]}},
                {"type": "assistant", "uuid": "c3", "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "internal reasoning"}, {"type": "text", "text": "Review the source"}]}}]
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual([m["content"] for m in parse_session(self.path, "claude-code")["messages"]], ["Find the battery plan", "Review the source"])

    def test_refresh_requires_matching_explicit_configuration_and_single_writer(self):
        class State:
            state = self.root / "workspace" / "state.sqlite"
            history = self.db
        state = State()
        self.assertEqual(configured_refresh(state), {"enabled": False})
        private_json(state.state.parent / "local-import-config.json", {"enabled": True,
            "history": str(self.root / "wrong.db"), "roots": {"codex": [str(self.sessions)]}})
        with self.assertRaisesRegex(ValueError, "different library"):
            configured_refresh(state)
        with import_lease(self.root / "lease"):
            with self.assertRaisesRegex(ValueError, "already running"):
                with import_lease(self.root / "lease"):
                    self.fail("The second writer acquired the same cache")

    def test_running_dashboard_picks_up_new_native_message_without_model(self):
        workspace = Workspace(self.db, self.root / "workspace" / "topics.sqlite")
        private_json(workspace.state.parent / "local-import-config.json", {"enabled": True,
            "history": str(self.db), "roots": {"codex": [str(self.sessions)]}})
        configured_refresh(workspace)
        workspace.sync()
        server = make_server(workspace, port=0, watch=True, interval=0.02, local_interval=0.05)
        try:
            with self.path.open("a") as stream:
                stream.write(json.dumps(self.message("automatic", "user", "New automatic battery update")) + "\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if workspace.overview()["messages"] == 4:
                    break
                time.sleep(0.02)
            self.assertEqual(workspace.overview()["messages"], 4)
            self.assertEqual(workspace.overview()["local_import"]["import"]["messages_added"], 1)
            with sqlite3.connect(self.db) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE content='New automatic battery update'").fetchone()[0], 1)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
