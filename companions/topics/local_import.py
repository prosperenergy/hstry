#!/usr/bin/env python3
"""Incrementally copy visible local session text into two dedicated HSTRY sources.

Explicit invocation only. Originals are read-only. Existing imported message
prefixes are immutable: a rewritten source fails closed rather than deleting or
silently replacing evidence. Tool, developer, system, and reasoning text is excluded.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from topics import now

SOURCES = {"codex": "local-codex-visible", "claude-code": "local-claude-code-visible"}
PARSER_VERSION = 3


@contextlib.contextmanager
def import_lease(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = cache / ".refresh-lock"
    try:
        lease.mkdir(mode=0o700)
    except FileExistsError:
        raise ValueError("A local import is already running; retry after it finishes")
    try:
        yield
    finally:
        lease.rmdir()


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temporary, path)


def timestamp(value):
    if not value:
        return None
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content if isinstance(p, dict)
                     and p.get("type") in ("text", "input_text", "output_text")
                     and isinstance(p.get("text"), str))


def bootstrap(content):
    return any(marker in content for marker in ("# AGENTS.md", "# Agent Configuration", "<available_skills>",
        "Guidance for coding agents", "<SYSTEM_PROMPT>", "</SYSTEM_PROMPT>",
        "The conversation history before this point was compacted", "<environment_context>"))


def parse_session(path, provider):
    messages, thread_id, created, workspace = [], None, None, None
    skipped, incomplete = 0, False
    seen = set()
    with path.open("rb") as stream:
        while True:
            line = stream.readline(32 * 1024 * 1024)
            if not line:
                break
            if not line.endswith(b"\n"):
                # Do not retain a partial record from an actively appended log.
                if len(line) < 32 * 1024 * 1024:
                    incomplete = True
                    break
                raise ValueError("Oversized session record; explicit review required")
            event = json.loads(line)
            if provider == "codex":
                payload = event.get("payload", {})
                if event.get("type") == "session_meta" and thread_id is None:
                    meta = payload.get("meta", payload)
                    if meta.get("thread_source") == "subagent" or isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
                        return None
                    thread_id = meta.get("id") or meta.get("session_id")
                    created = timestamp(meta.get("timestamp") or event.get("timestamp"))
                    workspace = meta.get("cwd")
                if event.get("type") != "response_item" or payload.get("type") != "message":
                    continue
                message, native_id = payload, payload.get("id")
            else:
                if event.get("type") not in ("user", "assistant"):
                    continue
                if event.get("isSidechain"):
                    skipped += 1
                    continue
                thread_id = thread_id or event.get("sessionId")
                created = created or timestamp(event.get("timestamp"))
                workspace = workspace or event.get("cwd")
                message, native_id = event.get("message", {}), event.get("uuid")
            role = message.get("role")
            if role not in ("user", "assistant"):
                skipped += 1
                continue
            content = text_content(message.get("content"))
            # Claude wraps tool results in user records; content filtering above
            # excludes them. Empty/tool-only records are not visible chat text.
            if not content.strip() or (role == "user" and bootstrap(content)):
                skipped += 1
                continue
            if native_id and native_id in seen:
                continue
            if native_id:
                seen.add(native_id)
            messages.append({"role": role, "content": content,
                             "created_at": timestamp(event.get("timestamp")),
                             "native_message_id": native_id})
    if not messages:
        return None
    if not thread_id or not created:
        raise ValueError("Session has no established identity or date")
    # This is a derived label, not a rename or a claimed original provider title.
    first = next((m["content"] for m in messages if m["role"] == "user"), "Local conversation")
    title = "[Local " + provider + "] " + " ".join(first.split())[:100]
    return {"id": thread_id, "created_at": created, "updated_at": max(
        m["created_at"] or created for m in messages), "workspace": workspace,
        "title": title, "messages": messages, "source_file": str(path),
        "excluded_records": skipped, "partial_tail_pending": incomplete}


def collect(roots, cache):
    cache = Path(cache).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = cache / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    binding = {name: [str(Path(p).expanduser().resolve()) for p in paths] for name, paths in roots.items()}
    if old and old.get("roots") != binding:
        raise ValueError("Import cache belongs to different source roots")
    report = {"checked_at": now(), "files": {}, "errors": [], "scanned": 0, "unchanged": 0,
              "parser_version": PARSER_VERSION, "excluded_files": [],
              "roots": binding, "coverage": {}, "model_calls": 0}
    conversations = {provider: {} for provider in roots}
    for provider, paths in binding.items():
        found = sorted({p for root in paths for p in Path(root).rglob("*.jsonl")})
        for path in found:
            key = hashlib.sha256(str(path).encode()).hexdigest()
            cached = cache / (key + ".json")
            stat = path.stat()
            signature = [stat.st_ino, stat.st_size, stat.st_mtime_ns]
            previous = old.get("files", {}).get(str(path), {})
            try:
                if old.get("parser_version") == PARSER_VERSION and previous.get("signature") == signature and cached.exists() and not previous.get("partial_tail_pending"):
                    conv = json.loads(cached.read_text())
                    report["unchanged"] += 1
                else:
                    conv = parse_session(path, provider)
                    after = path.stat()
                    if signature != [after.st_ino, after.st_size, after.st_mtime_ns]:
                        raise ValueError("Session changed during reading; retry on next refresh")
                    private_json(cached, conv)
                    report["scanned"] += 1
                if conv is None:
                    report["excluded_files"].append(str(path))
                    report["files"][str(path)] = {"signature": signature, "partial_tail_pending": False}
                    continue
                existing = conversations[provider].get(conv["id"])
                if existing and existing["messages"] != conv["messages"]:
                    raise ValueError("Conflicting copies of one session identity")
                conversations[provider][conv["id"]] = conv
                report["files"][str(path)] = {"signature": signature, "partial_tail_pending": conv["partial_tail_pending"]}
            except (ValueError, OSError) as exc:
                report["errors"].append({"source_file": str(path), "reason": str(exc)})
        report["coverage"][provider] = {"files_found": len(found), "sessions_parsed": len(conversations[provider]),
            "visible_messages": sum(len(c["messages"]) for c in conversations[provider].values()),
            "scope": "Local user/assistant text only; tools, attachments, system context, sidechains, and cloud-only history excluded"}
    private_json(manifest_path, report)
    return conversations, report


def digest_other_sources(db):
    result = hashlib.sha256()
    placeholders = ",".join("?" for _ in SOURCES)
    for sql in ("SELECT * FROM sources WHERE id NOT IN (" + placeholders + ") ORDER BY id",
                "SELECT * FROM conversations WHERE source_id NOT IN (" + placeholders + ") ORDER BY id",
                "SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.source_id NOT IN (" + placeholders + ") ORDER BY m.id"):
        for row in db.execute(sql, tuple(SOURCES.values())):
            result.update(json.dumps(list(row), ensure_ascii=False).encode())
    return result.hexdigest()


def apply(history, conversations, report, backup_dir, dry_run=True):
    history = Path(history).expanduser().resolve()
    if report["errors"]:
        raise ValueError("Source read errors require review before importing")
    db = sqlite3.connect(history.as_uri() + "?mode=rw", uri=True, timeout=30)
    db.execute("PRAGMA foreign_keys=ON")
    additions = []
    try:
        db.execute("BEGIN IMMEDIATE")
        before = digest_other_sources(db)
        for provider, sessions in conversations.items():
            source = SOURCES[provider]
            existing_source = db.execute("SELECT adapter FROM sources WHERE id=?", (source,)).fetchone()
            if existing_source and existing_source[0] != "local-visible-copy-v1":
                raise ValueError("Local import source identity mismatch")
            for conv in sessions.values():
                row = db.execute("SELECT id FROM conversations WHERE source_id=? AND external_id=?", (source, conv["id"])).fetchone()
                cid = row[0] if row else str(uuid.uuid5(uuid.NAMESPACE_URL, source + ":" + conv["id"]))
                old = db.execute("SELECT idx,role,content FROM messages WHERE conversation_id=? ORDER BY idx", (cid,)).fetchall()
                prefix = [(i, m["role"], m["content"]) for i, m in enumerate(conv["messages"][:len(old)])]
                if old != prefix:
                    raise ValueError("Imported message prefix changed; preserving existing evidence")
                if not row or len(old) < len(conv["messages"]):
                    additions.append((provider, source, cid, conv, len(old)))
        summary = {"sessions_changed": len(additions), "messages_added": sum(len(c["messages"]) - n for _, _, _, c, n in additions),
                   "dry_run": dry_run, "model_calls": 0}
        if dry_run or not additions:
            db.rollback()
            return summary
        backup_dir = Path(backup_dir).expanduser().resolve()
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path = backup_dir / ("history-before-local-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite")
        # Backup from a separate read connection while this writer owns the lease.
        with sqlite3.connect(history.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(backup_path) as backup:
            src.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity failed")
        backup_path.chmod(0o600)
        for provider, source, cid, conv, count in additions:
            db.execute("INSERT OR IGNORE INTO sources(id,adapter,path,config) VALUES(?,?,?,?)", (
                source, "local-visible-copy-v1", json.dumps(report["roots"][provider]),
                json.dumps({"scope": report["coverage"][provider]["scope"]})))
            meta = {"native_thread_id": conv["id"], "source_file": conv["source_file"],
                    "source_kind": "local-visible-session-copy", "title_is_derived": True,
                    "source_scope": report["coverage"][provider]["scope"], "copied_at": report["checked_at"]}
            db.execute("""INSERT INTO conversations(id,source_id,external_id,title,created_at,updated_at,workspace,metadata,provider,harness)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,metadata=excluded.metadata,version=conversations.version+1""",
                (cid, source, conv["id"], conv["title"], conv["created_at"], conv["updated_at"], conv["workspace"], json.dumps(meta), provider, provider))
            for idx, message in enumerate(conv["messages"][count:], count):
                mid = str(uuid.uuid5(uuid.NAMESPACE_URL, source + ":" + conv["id"] + ":" + str(idx)))
                db.execute("INSERT INTO messages(id,conversation_id,idx,role,content,created_at,metadata) VALUES(?,?,?,?,?,?,?)", (
                    mid, cid, idx, message["role"], message["content"], message["created_at"],
                    json.dumps({"native_message_id": message["native_message_id"], "source_file": conv["source_file"], "historical_evidence_only": True})))
            db.execute("UPDATE conversations SET message_count=(SELECT count(*) FROM messages WHERE conversation_id=?) WHERE id=?", (cid, cid))
            db.execute("UPDATE sources SET last_sync_at=? WHERE id=?", (int(time.time()), source))
        if digest_other_sources(db) != before:
            raise ValueError("Unrelated source changed; rolling back")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Library integrity failed; rolling back")
        db.commit()
        summary.update(backup=str(backup_path), unrelated_sources_unchanged=True)
        return summary
    finally:
        db.rollback()
        db.close()


def configured_refresh(workspace):
    """Opt-in importer for explicit dashboard refresh or its running watcher."""
    config_path = workspace.state.parent / "local-import-config.json"
    if not config_path.exists():
        return {"enabled": False}
    config = json.loads(config_path.read_text())
    if config.get("enabled") is not True:
        return {"enabled": False}
    if Path(config["history"]).expanduser().resolve() != workspace.history:
        raise ValueError("Local import configuration belongs to a different library")
    roots = config["roots"]
    if not isinstance(roots, dict) or not roots or set(roots) - set(SOURCES):
        raise ValueError("Invalid local source configuration")
    if any(not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths) for paths in roots.values()):
        raise ValueError("Local sources require explicit directory paths")
    cache = workspace.state.parent / "local-import"
    with import_lease(cache):
        conversations, report = collect(roots, cache)
        if report["errors"]:
            raise ValueError(str(len(report["errors"])) + " local session files need review; previous library retained")
        result = apply(workspace.history, conversations, report, cache / "backups", False)
        report["import"] = result
        private_json(cache / "latest-result.json", report)
    status = {"enabled": True, "checked_at": report["checked_at"], "coverage": report["coverage"],
              "scanned": report["scanned"], "unchanged": report["unchanged"], "import": result,
              "refresh_seconds": 300, "excluded_files": len(report["excluded_files"])}
    status["import"].pop("backup", None)
    with workspace.db() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('local_import',?)", (json.dumps(status),))
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--claude-home", default=str(Path.home() / ".claude"))
    parser.add_argument("--apply", action="store_true", help="Back up and append verified visible text; default is dry-run")
    args = parser.parse_args()
    codex, claude = Path(args.codex_home).expanduser(), Path(args.claude_home).expanduser()
    roots = {"codex": [codex / "sessions", codex / "archived_sessions"], "claude-code": [claude / "projects"]}
    with import_lease(args.cache):
        conversations, report = collect(roots, args.cache)
        if report["errors"]:
            private_json(Path(args.cache) / "latest-result.json", report)
            raise ValueError(str(len(report["errors"])) + " session files need review; see private manifest")
        result = apply(args.history, conversations, report, Path(args.cache) / "backups", dry_run=not args.apply)
        report["import"] = result
        private_json(Path(args.cache) / "latest-result.json", report)
    print(json.dumps({"coverage": report["coverage"], "scanned": report["scanned"], "unchanged": report["unchanged"], "import": result}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
