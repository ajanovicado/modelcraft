# Privacy Policy — Modelcraft

**Last updated: 2026-10-14**

## Summary

Modelcraft runs model inference and workspace analysis locally. The application
does not send telemetry, chat content or workspace files to its developer or a
model provider. User-configured database sources communicate with the selected
data provider; model setup can download the pinned model when requested.

## What the application accesses

- **Local files you explicitly import** (CSV, Excel, Power BI Project files,
  Power BI report files `.pbix`) via the system file picker. These are read and
  profiled on-device only.
- **Imported `.pbix` reports can be edited.** Studio copies the file into its
  own folder and keeps every edited version there as a separate file, so each
  change can be undone. Your original file is not modified unless you choose
  "Save to file…" and confirm; a backup is kept next to it first. "Save a copy…"
  writes a new file at a location you choose. Editing measures opens your
  installed Power BI Desktop on the working copy; nothing is sent anywhere.
- **Local AI runtime**: an owned, CPU-only Ollama process with
  integrity-verified model files installed on your device. Inference runs entirely on your machine;
  the runtime is configured with cloud access disabled (`OLLAMA_NO_CLOUD=1`).
- **Optional local tools**: if installed, Power BI Desktop and the `pbi-cli`
  command-line tool are invoked locally to open or validate generated
  projects. They are never contacted with your data by this application.

## Network behavior

- The desktop shell talks to a Python sidecar over **loopback only**
  (`127.0.0.1`, ephemeral port) with a per-launch authentication token.
- Chat, data analysis and report editing run locally and do not send your
  questions, formulas or imported data to a model provider.
- When you configure a SQL Server/Azure SQL or Snowflake source, the selected
  provider receives the authentication and read requests needed for that source.
  Credentials remain in vendor-managed profiles. Imported snapshots are then
  analyzed locally. The provider's own privacy terms apply to its service.
- If the model is missing, you can choose a one-time download from Ollama's
  model registry in Settings, or install a matching model file from your PC.
  A download sends a normal HTTPS request to the registry and its download
  host; it includes no workspace files or chat content. The downloaded model
  must match the application's pinned digest before it can be used.
- The installer may download WebView2 on machines that lack it. After setup,
  the local inference and workspace features can operate offline.

## Data stored on your device

Workspaces, drafts, chat transcripts, model evidence, and the working copies
and edit history of imported reports are stored under:

```
%LOCALAPPDATA%\AI Power BI Analyst\
```

Main and DAX chat history belongs to the selected workspace. Starting a new
chat keeps the previous conversation for reference and removes it from the
new chat's context. Questions you have typed but not sent, and recovery
information for interrupted requests, are also cached in the desktop webview's
local storage. In browser development mode, that cache belongs to the local
development site's browser storage.

Deleting a workspace in the app removes its saved transcripts, assistant
request receipts, snapshots, drafts and working report copies. Exported
copies and original source files are retained. Browser or webview caches
can also be removed through the host's storage controls.

Uninstalling the application does not delete this folder; remove it manually
to erase all application data.

## Changes

Policy updates ship with the application and are posted in the public GitHub
repository.

## Contact

For support, use [Modelcraft's GitHub issues](https://github.com/adomaticoo/modelcraft/issues).
Do not include private data or credentials in a public issue. See `SECURITY.md`
for private vulnerability reporting.
