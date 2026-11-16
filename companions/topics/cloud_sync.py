#!/usr/bin/env python3
"""Copy a private, visible-text projection to Brain; originals remain local."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from local_import import private_json
from topics import Workspace, now

MAX_BATCH = 400000
MAX_CHUNK = 100000
SECRET_PATTERNS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|sb_secret_[A-Za-z0-9_-]{20,})\b",
    r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}",
    r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b",
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scrub(text):
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[credential redacted]", text)
    return text


def record(record_id, kind, payload):
    value = canonical(payload)
    return {"record_id": record_id, "kind": kind, "payload_json": value,
            "content_hash": hashlib.sha256(value.encode()).hexdigest()}


def projection(workspace):
    """Yield immutable chunks first, their header second, and a receipt last."""
    totals = {"entries": 0, "messages": 0, "reference_entries": 0}
    with workspace.source() as src, workspace.db() as state:
        for chat in state.execute("SELECT * FROM chats WHERE present=1 ORDER BY id"):
            chunks, pending = [], []
            count = 0
            redacted = False

            def make_chunk(messages):
                payload = {"chat_id": chat["id"], "messages": messages}
                digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
                chunk = record("chunk:" + chat["id"] + ":" + digest, "chunk", payload)
                chunks.append(chunk["record_id"])
                return chunk

            for message in src.execute(
                    "SELECT id,idx,role,content,created_at FROM messages WHERE conversation_id=? ORDER BY idx,id",
                    (chat["id"],)):
                if message["role"] not in ("user", "assistant"):
                    continue
                content = message["content"] or ""
                safe = scrub(content)
                redacted = redacted or safe != content
                count += 1
                # Bound individual parts in bytes, including astral Unicode.
                parts = [safe[i:i + 6000] for i in range(0, len(safe), 6000)] or [""]
                for part, text in enumerate(parts):
                    item = {"id": message["id"], "index": message["idx"], "role": message["role"],
                            "created_at": message["created_at"], "part": part,
                            "parts": len(parts), "content": text}
                    if pending and len(canonical({"chat_id": chat["id"], "messages": pending + [item]}).encode()) > MAX_CHUNK:
                        yield make_chunk(pending)
                        pending = []
                    pending.append(item)
            if pending:
                yield make_chunk(pending)
            memberships = [dict(row) for row in state.execute(
                "SELECT topic_id,kind FROM assignments WHERE chat_id=? AND kind!='excluded' ORDER BY topic_id", (chat["id"],))]
            payload = {"chat_id": chat["id"], "title": scrub(chat["title"]), "source": chat["source"],
                       "source_url": chat["source_url"], "external_id": chat["external_id"],
                       "created_at": chat["created_at"], "updated_at": chat["updated_at"],
                       "present": bool(chat["present"]), "reference_only": bool(chat["reference_only"]),
                       "message_count": count, "chunks": chunks, "memberships": memberships,
                       "topic_ids": [m["topic_id"] for m in memberships], "credentials_redacted": redacted}
            yield record("conversation:" + chat["id"], "conversation", payload)
            if chat["present"]:
                totals["entries"] += 1
                totals["messages"] += count
                totals["reference_entries"] += int(bool(chat["reference_only"]))
    overview = workspace.overview()
    for topic in overview["topics"]:
        context = workspace.context(topic["id"])
        # Do not copy local filesystem settings, importer manifests, or tools.
        payload = {"topic_id": topic["id"], "title": topic["name"],
                   "brief": scrub(context["brief"]), "brief_saved_at": context["brief_saved_at"],
                   "brief_needs_review": context["brief_needs_review"],
                   "brief_evidence": context["brief_evidence"], "canonical": context["canonical"],
                   "instructions": context["instructions"]}
        yield record("topic:" + topic["id"], "topic", payload)
    # Keep upstream coverage partial/unverified; successful upload is not complete cloud coverage.
    coverage = overview["source_coverage"]
    yield record("collector-status", "status",
                 {"title": "Collector status", "checked_at": now(), "counts": totals,
                  "history_checked_at": (overview["last_sync"] or {}).get("checked_at"),
                  "source_coverage": coverage, "sources": overview["sources"],
                  "native_changes": overview["native_changes"], "all_ai_complete": False})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def destination(config):
    parsed = urlparse(config["endpoint"])
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path != "/api/chat-library/sync"):
        raise ValueError("Use an exact HTTPS Brain /api/chat-library/sync endpoint")
    expected = config.get("expected_host")
    if parsed.hostname != expected:
        raise ValueError("Brain hostname must match the explicitly configured expected_host")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", config.get("collector_id", "")):
        raise ValueError("Set a stable non-personal collector_id")
    if not re.fullmatch(r"[a-z]{20}", config.get("project_ref", "")):
        raise ValueError("Set the verified Supabase project_ref")
    return config["endpoint"]


def post_batch(config, batch, opener=None, sleep=time.sleep):
    endpoint = destination(config)
    token = os.environ.get(config.get("token_env", "HSTRY_BRAIN_SYNC_TOKEN"), "")
    if not token and config.get("token_file"):
        path = Path(config["token_file"]).expanduser()
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("The dedicated upload token file must be owned by you with mode 0600")
            token = handle.read(4096).strip()
    if len(token) < 32:
        raise ValueError("Configure the dedicated Brain upload token; never use a Supabase service key")
    data = canonical({"collector_id": config["collector_id"], "project_ref": config["project_ref"],
                      "records": batch}).encode()
    opener = opener or build_opener(NoRedirect())
    for attempt in range(4):
        request = Request(endpoint, data=data, method="POST",
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with opener.open(request, timeout=45) as response:
                receipt = json.loads(response.read(100000))
            expected = {r["record_id"]: r["content_hash"] for r in batch}
            if receipt.get("project_ref") != config["project_ref"] or receipt.get("collector_id") != config["collector_id"]:
                raise ValueError("Upload receipt belongs to a different destination")
            if receipt.get("receipts") != expected:
                raise ValueError("Upload was not confirmed; queue retained for retry")
            return receipt
        except HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 3:
                raise ValueError("Brain upload HTTP " + str(exc.code) + "; queue retained") from None
        except (URLError, TimeoutError):
            if attempt == 3:
                raise ValueError("Brain upload unavailable; queue retained") from None
        sleep(2 ** attempt)
    raise ValueError("Upload unavailable")


def sync(workspace, config, cache, apply=False, send=post_batch):
    destination(config)
    cache = Path(cache)
    checkpoint = cache / "cloud-checkpoint.json"
    previous = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    identity = {key: config[key] for key in ("endpoint", "expected_host", "collector_id", "project_ref")}
    if previous and previous.get("destination") != identity:
        raise ValueError("Cloud checkpoint belongs to a different destination; use a new cache")
    hashes = dict(previous.get("hashes", {}))
    changed = sent = total = 0
    batch = []

    def flush():
        nonlocal sent, batch
        if apply and batch:
            send(config, batch)
            for item in batch:
                hashes[item["record_id"]] = item["content_hash"]
            # Acknowledged batches survive a later network failure.
            private_json(checkpoint, {"destination": identity, "hashes": hashes, "checked_at": now()})
            sent += len(batch)
        batch = []

    for item in projection(workspace):
        total += 1
        if hashes.get(item["record_id"]) == item["content_hash"]:
            continue
        changed += 1
        if batch and (len(batch) >= 100 or len(canonical({"records": batch + [item]}).encode()) > MAX_BATCH):
            flush()
        if len(canonical(item).encode()) > MAX_BATCH:
            raise ValueError("Record exceeds upload bound; previous cloud records retained")
        batch.append(item)
    flush()
    result = {"checked_at": now(), "dry_run": not apply, "records": total,
              "changed": changed, "uploaded_and_verified": sent, "all_ai_complete": False,
              "project_ref": config["project_ref"], "collector_id": config["collector_id"]}
    if apply:
        private_json(cache / "latest-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--state")
    parser.add_argument("--config", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = json.loads(Path(args.config).expanduser().read_text())
    print(json.dumps(sync(Workspace(args.history, args.state), config, args.cache, args.apply)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
