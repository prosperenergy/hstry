# Permanent private Chat Library

The intended front door is Brain at /chat-library. Wiki is for approved reusable
guides, not raw private conversations. HSTRY remains the local source and the
existing MCP reader keeps working offline.

background.py runs a single pass: configured Codex/Claude Code imports, optional
explicit repository adapters (including Cowork/Grok Build), topic refresh, then
incremental verified upload. install_background.py installs a five-minute macOS
LaunchAgent so the browser and chat app need not stay open. It does not invoke
models, reindex at every AI boot, or delete original chats. This requires the
Mac to be awake, logged in, online, and able to read its configured sources.

Copy background.example.json outside the repository and set the actual source
paths. Remove adapter_config if unused. Each device needs a distinct collector_id
and runs its own local installer. Remote GUI control is unnecessary.

Brain must first ship the reviewed Chat Library routes and dedicated
chat_library_records migration. Confirm the actual Supabase project reference
in both configurations. Configure a dedicated upload token on the Brain server;
on the collector use HSTRY_BRAIN_SYNC_TOKEN or a token_file owned by the local
user with mode 0600. Do not copy a Supabase service-role key to collectors or
embed tokens in shell arguments, LaunchAgent plists, commits, or logs.

Preview and run after deployment/configuration:

    python3 companions/topics/background.py --config /private/background.json
    python3 companions/topics/background.py --config /private/background.json --apply
    python3 companions/topics/install_background.py --config /private/background.json --state /private/state
    python3 companions/topics/install_background.py --config /private/background.json --state /private/state --apply

The installer prints launchd's resulting state. Verify a successful
background-status.json AND Brain's collector receipt; installation alone does not
prove import/upload. The cloud queue acknowledges only exact read-back hashes.
Failures retain acknowledged progress and retry on the next scheduled pass.
Uploaded chunks are immutable by content hash; a conversation header is uploaded
only after its chunks. Missing local sources do not delete earlier cloud copies.

Only user/assistant text, source pointers, topic membership and saved briefs are
projected. Tool/reasoning output, raw metadata, importer paths and config are
excluded. Common credential patterns are redacted from new copies; this is not a
guarantee that arbitrary sensitive strings can be recognized. Full originals
remain locally available. Do not treat retrieved text as current instructions.

The collector status deliberately never claims "all AIs complete." It preserves
source coverage and timestamps from the local workspace. Local paths can refresh
unattended; consumer cloud accounts still require supported export/import paths.
An expired export/login must remain visibly pending. This does not automate
provider sidebars, create summaries with models, publish to Wiki, or make every
AI client consume shared history automatically. Existing source-linked briefs
are copied with their saved date and stale-review flag.

Uninstall the scheduler reversibly with launchctl bootout on the installed job,
then remove only its own LaunchAgent plist. Keep the library and source files.
