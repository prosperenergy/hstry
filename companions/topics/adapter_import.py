#!/usr/bin/env python3
"""Prepare visible local history with repository adapters, then append safely.

Explicit configuration only. This never signs in, activates a model, or edits
provider stores. Tool and reasoning parts are excluded from the stored copy.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from local_import import apply, bootstrap, derived_title, import_lease, private_json
from topics import now


def seconds(value):
    return int(value // 1000) if isinstance(value, (int, float)) and value > 0 else None


def prepare(provider, root, rows):
    if not isinstance(rows, list):
        raise ValueError("Adapter did not return a conversation list")
    conversations = {}
    excluded = empty = 0
    for row in rows:
        identity = row.get("externalId")
        created = seconds(row.get("createdAt"))
        if not identity or not created:
            raise ValueError("Adapter conversation has no source identity or date")
        messages = []
        for message in row.get("messages", []):
            role = message.get("role")
            parts = message.get("parts")
            if isinstance(parts, list) and parts:
                content = "\n".join(p["text"] for p in parts if p.get("type") == "text"
                                    and isinstance(p.get("text"), str))
            else:
                content = message.get("content", "")
            if (role not in ("user", "assistant") or not isinstance(content, str)
                    or not content.strip() or role == "user" and bootstrap(content)):
                excluded += 1
                continue
            messages.append({"role": role, "content": content,
                "created_at": seconds(message.get("createdAt")),
                "native_message_id": message.get("id")})
        if not messages:
            empty += 1
            continue
        metadata = row.get("metadata") or {}
        source_file = metadata.get("file") or str(root)
        conversation = {"id": identity, "created_at": created,
            "updated_at": seconds(row.get("updatedAt")) or max(m["created_at"] or created for m in messages),
            "workspace": row.get("workspace"), "messages": messages, "source_file": source_file,
            "title": "[Local " + provider + "] " + row["title"] if row.get("title") else derived_title(messages, provider),
            "title_source_file": source_file if row.get("title") else None}
        if identity in conversations and conversations[identity] != conversation:
            raise ValueError("Adapter returned conflicting copies of one conversation")
        conversations[identity] = conversation
    return conversations, {"parsed_conversations": len(rows), "visible_conversations": len(conversations),
        "visible_messages": sum(len(c["messages"]) for c in conversations.values()),
        "excluded_messages": excluded, "empty_conversations": empty,
        "scope": "Visible local user/assistant text prepared by the named repository adapter; tools, reasoning, system context and attachments excluded; cloud-only history not covered"}


def run(history, config, cache, bun, dry_run=True):
    config = json.loads(Path(config).read_text())
    if not isinstance(config, list) or not config:
        raise ValueError("Configure explicit adapter, provider, source_id and path entries")
    sources, conversations = {}, {}
    report = {"checked_at": now(), "roots": {}, "coverage": {}, "errors": [], "model_calls": 0}
    adapters = Path(__file__).resolve().parents[2] / "adapters"
    with import_lease(cache):
        for item in config:
            name, provider, source_id = item["adapter"], item["provider"], item["source_id"]
            if not re.fullmatch(r"[a-z0-9-]+", name) or provider in sources or source_id in sources.values():
                raise ValueError("Adapter names and provider source identities must be distinct")
            path = Path(item["path"]).expanduser().resolve()
            adapter = adapters / name / "adapter.ts"
            if not path.exists() or not adapter.is_file():
                raise ValueError("Configured source or repository adapter is missing")
            request = json.dumps({"method": "parse", "params": {"path": str(path), "opts": {}}})
            result = subprocess.run([bun, "run", str(adapter)], capture_output=True, text=True,
                env=dict(os.environ, HSTRY_REQUEST=request), timeout=180)
            if result.returncode:
                raise ValueError("Adapter failed; source retained: " + name)
            prepared, coverage = prepare(provider, path, json.loads(result.stdout))
            sources[provider] = source_id
            conversations[provider] = prepared
            report["roots"][provider] = [str(path)]
            report["coverage"][provider] = dict(coverage, adapter=name, checked_at=now())
        private_json(Path(cache) / "prepared-visible.json", conversations)
        report["import"] = apply(history, conversations, report, Path(cache) / "backups",
                                 dry_run=dry_run, source_ids=sources)
        private_json(Path(cache) / "latest-result.json", report)
    return {"coverage": report["coverage"], "import": report["import"], "checked_at": report["checked_at"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--bun", default="bun")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.history, args.config, args.cache, args.bun, not args.apply)))


if __name__ == "__main__":
    main()
