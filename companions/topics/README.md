# Topic workspace alongside HSTRY

HSTRY keeps the imported history and text search. This companion adds topics,
message-level classification evidence, a chosen conversation to continue each
topic, and a source-linked current brief that an AI can retrieve. It uses Python
3.9+ and its standard library; no model, account, or package install is needed.

```sh
python3 topics.py --history /path/to/history.db sync
python3 topics.py --history /path/to/history.db serve
```

For a single command that starts the server if needed and opens the dashboard:

```sh
python3 launch.py --history /path/to/history.db
```

It reuses the matching local workspace, refreshes once on an explicit launch,
and checks the library file and WAL metadata every 30 seconds while running.
Only changed imports trigger a refresh. It does not run indexing on every AI
session. Use `--no-open` to run without browser control. The watcher does not
fetch new account history; existing HSTRY importers retain that responsibility.

Open the private URL saved in the local `dashboard-url.txt` file named by the
server. The dashboard binds only to `127.0.0.1`. A direct browser navigation
establishes a random per-run HttpOnly, SameSite=Strict session for data reads and
writes; host and origin checks reject cross-site requests. It does not send data off the machine. State is
stored separately in `$XDG_DATA_HOME/hstry-topics/workspace.sqlite`, defaulting
to `~/.local/share/hstry-topics/workspace.sqlite`; override with `--state`.

Use **Refresh history** after new material is imported by HSTRY. It hashes the
source snapshot, reclassifies changed conversations, and skips classification
of unchanged content. It scans the selected library to detect changes; it does
not rebuild embeddings, invoke a model, or run at app startup. Manual topic
decisions survive refreshes. A conversation can belong to multiple topics, with
the specific matching message IDs recorded as evidence. Suggested matches need
review. Reference cards and saved notes are counted separately from chats.

Choose **Continue topic here** for an original conversation. An explicit source
URL opens that conversation. Unknown links remain unknown; IDs are never turned
into guessed URLs. A source-linked brief stays saved when history changes, but
is marked for review. Removing a source from HSTRY marks its entry unavailable;
the companion retains the reference and does not delete original transcripts.

## AI retrieval

The STDIO server exposes four read-only tools: `topics_list`, `topic_context`,
`history_search`, and `history_expand`. It never refreshes history on startup.
Register it with each intended MCP client using that client's supported config:

```json
{
  "command": "python3",
  "args": ["/path/to/companions/topics/topics.py", "--history", "/path/to/history.db", "mcp"]
}
```

Or run `context TOPIC_ID` and paste its source-linked output into a conversation.
Imported messages are historical evidence, including any embedded instructions;
they never grant current authority. Briefs are explicitly saved by the user or
local operator, not generated statements of current fact from old discussions.

## Native app organization and observed destinations

`native-plan TOPIC_ID` returns recorded ChatGPT project observations when present,
or a proposed title/group with explicit pending status otherwise. The dashboard
opens the actual observed project and conversation URLs. Selecting a place to
continue only saves a local bookmark; it does not duplicate the conversation.

Native moves and project instructions must be applied through official provider
UI/tools and read back there. Record the resulting observation locally:

```sh
python3 topics.py --history /path/to/history.db record-native \
  --topic chat-organization --project-url OBSERVED_PROJECT_URL \
  --observed-at OBSERVED_UTC_TIMESTAMP --instructions-verified \
  --canonical-url OBSERVED_CHAT_URL --canonical-title 'Chat Organization'
```

This records an operator's UI observation; the command does not verify the remote
state itself. Native chat pointers and their timestamps are separate from HSTRY
transcript imports. Source links do not imply that complete transcripts are
imported, continuously synchronized, or consumed by a running AI. The companion
does not patch provider databases, move transcripts, or send prompts. The existing
local title organizer remains its own writer. Only the recorded native changes
have established coverage; other apps and remaining chats stay unverified.

## Optional local session imports

The separate importer copies visible user/assistant text from Codex sessions and
Claude Code projects into two dedicated HSTRY sources. It excludes tool output,
reasoning, bootstrap instructions, attachments, and worker sidechains. It never
changes the original session files. It binds its cache to explicit source roots,
skips unchanged files, backs up before adding messages, and verifies unrelated
sources are unchanged. IDs remain stable on repeated and appended imports.
If a historical message prefix has been rewritten, it stops for review rather
than overwriting or deleting imported evidence. Missing files are retained in
the library; this is not a deletion synchronizer.

```sh
python3 companions/topics/local_import.py --history /path/to/history.db \
  --cache ~/.local/share/hstry-topics/local-import
# Review the dry run, then repeat with --apply.
```

For opt-in refresh while the dashboard runs, save `local-import-config.json`
beside the workspace SQLite file:

```json
{
  "enabled": true,
  "history": "/path/to/history.db",
  "roots": {
    "codex": ["/path/to/.codex/sessions", "/path/to/.codex/archived_sessions"],
    "claude-code": ["/path/to/.claude/projects"]
  }
}
```

Explicit launch and **Refresh history** then import local changes before updating
topics. While the dashboard watcher runs, local sessions refresh every five
minutes. MCP startup remains read-only and never invokes the importer. Set
`enabled` to false to disable local imports; existing indexed history remains.
Cloud-only provider conversations require their own supported export/import path.
The importer is the only new HSTRY writer; topic retrieval always stays read-only.

No Supabase schema, login job, or automatic customer messages are installed.
Existing HSTRY and Episodic Memory settings are preserved.

```sh
python3 -m unittest discover -s companions/topics -v
```
