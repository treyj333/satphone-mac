# SATPHONE for macOS

SATPHONE is a community macOS application for operating and diagnosing a Blues Notecard + StarNote satellite messaging setup. It combines the old SATPHONE terminal and Discord bridge into one double-clickable app.

The app provides:

- a dashboard for the Notecard, StarNote, Discord, and Notehub;
- messages in both directions, with optional automatic inbound sync after `/satphone`;
- read-only diagnostics and safe local self-repair;
- separately confirmed template and satellite-transport repairs;
- live Notehub `messages.qi` preview and selected-message deletion;
- a T-Deck-first ESP32-S3 firmware center;
- high-school-level setup, testing, and troubleshooting help;
- privacy-aware logs and diagnostic report export.

The app serializes every Notecard USB operation so two features cannot fight over the port. Permanent device changes, message deletion, and firmware flashing always require explicit confirmation.

## Download and open the Mac app

Download `SATPHONE-macOS-<architecture>.zip` from the GitHub Releases page, unzip it, and move `SATPHONE.app` to Applications.

This is an ad-hoc-signed community build, not an Apple-notarized App Store product. On first launch, Control-click `SATPHONE.app` in Finder, choose **Open**, then confirm.

Apple Silicon Macs use the `arm64` download. Intel Macs use the `X64`/`x86_64` download produced by GitHub Actions.

Application settings and logs live in:

```text
~/Library/Application Support/SATPHONE/
```

Discord and Notehub tokens remain in macOS Keychain. They are not saved in the repository or the JSON configuration.

## Build the app from source

Python 3.10 or newer is required. On Apple Silicon with Homebrew Python 3.12:

```bash
cd satphone-v0
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-build.txt
python -m unittest discover -v
PYTHON_BIN="$PWD/.venv/bin/python" zsh scripts/build_macos.sh
```

The build creates:

```text
dist/SATPHONE.app
release/SATPHONE-macOS-<architecture>.zip
```

The GitHub workflow runs the full simulated test suite, builds an unsigned/ad-hoc-signed Mac artifact for pull requests, and creates a draft GitHub Release when a `v*` tag is pushed.

## First-time app setup

1. Plug the Notecard/StarNote kit into the Mac and close the Blues browser terminal.
2. Open **Connections** and enter the Notehub ProjectUID and DeviceUID plus the Discord server/channel IDs.
3. Store the Discord bot token and an expiring Notehub Personal Access Token in Keychain.
4. Register `/satphone` once, then start the bridge.
5. Run **Full Diagnostic**. Review and separately approve any persistent template or transport repair.
6. In Discord, use `/satphone`; do not post an ordinary channel message. Leave **Auto receive** on or click **Receive Now**.

## Safety boundaries

- Safe automatic repair may refresh USB and restart this app's Discord bridge.
- The app never auto-deletes a message and never auto-flashes firmware.
- The Notehub maintenance page changes only selected live `messages.qi` Notes. It does not erase Notehub event history.
- T-Deck firmware flashing excludes Blues Notecard USB devices and requires an exact `.bin`, offset, acknowledgement, and typed confirmation.
- Notecard firmware uses the Blues-approved Notehub workflow; StarNote firmware remains a guided advanced procedure because some kits require additional update hardware.

## Legacy development CLI

The original command-line harness remains available for development and deep trace work. Do not run it at the same time as the Mac app because only one process can own Notecard USB.

## What it does

Run:

```bash
.venv/bin/python satphone.py
```

The interactive menu provides:

```text
SATPHONE DEVELOPMENT TOOL

1. Run full diagnostic
2. Send satellite message
3. Check for incoming satellite message
4. View incoming messages
5. Check GPS/location
6. Show StarNote status
7. Show transport configuration
8. Live debug mode
9. Export diagnostic report
0. Exit
```

The tool automatically looks for the official Blues USB vendor/product IDs, then falls back to Notecard-style macOS device names. Use an explicit port when needed:

```bash
.venv/bin/python satphone.py --port /dev/cu.usbmodemNOTE1
```

## Install

Python 3.10 or newer is required. On this Mac, plain `python3` resolves to Apple's Python 3.9, while Homebrew Python 3.12 is installed as `/opt/homebrew/bin/python3.12`. Name that interpreter explicitly when creating the environment:

```bash
cd ~/Desktop/satphone-v0
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python satphone.py
```

The Finder-friendly launchers use this folder's `.venv`. The requirements pin `note-python` 2.4.1, its maintained `filelock` dependency, PySerial, the desktop `python-periphery` transport imported by the SDK, and `discord.py` 2.7.1; they do not install the retired Python 3.9 dependency line.

Only one program can own the Notecard USB serial stream. Disconnect the Blues in-browser terminal before launching SATPHONE.

## Message schema

The reference implementation uses two unique compact NTN templates:

```json
{"req":"note.template","file":"messages.qo","format":"compact","port":57,"body":{"msg":"-"}}
```

```json
{"req":"note.template","file":"messages.qi","format":"compact","port":56,"body":{"msg":"-"}}
```

The brief specified inbound port 56 but did not specify an outbound template. This installation uses port 57 for `messages.qo` because the device's existing `sat.qo` template already owns port 55; NTN ports must be unique from 1–100. The `"-"` value is Blues' variable-length UTF-8 string type hint.

Template checks always use `verify:true`. A body-less `note.template` request without `verify:true` can clear a template, so the program never uses that form. Repairs display the current difference and require confirmation. Immediately before writing, the tool re-reads the template and refuses the change if its state has changed since it was displayed. It verifies the result after writing. A new or modified template must then be synced to Notehub once over cellular or Wi-Fi before NTN can use it.

Messages are limited to 160 UTF-8 bytes in this first reference schema. This is intentionally below the complete Skylo packet limit because the encoded packet also has protocol overhead.

## Discord bridge

The existing `Satphone → Discord` Notehub route handles the device-to-Discord direction:

```text
messages.qo → Notehub HTTP route → Discord incoming webhook
```

An incoming Discord webhook cannot read channel messages. The reverse direction uses a guild-scoped `/satphone` application command received over Discord's Gateway. The bridge enables only the non-privileged Guild Messages intent so it can warn when someone posts an ordinary message; the privileged Message Content intent remains disabled and ordinary message text is never read:

```text
/satphone message:hello → local bridge → Notehub Add QI Note → messages.qi
```

The bridge is locked to the configured server, channel, device, and `messages.qi`. The command defaults to administrators only; use **Server Settings → Integrations** to grant it to a trusted user or role and keep it limited to the `satphone-v0` channel. It does not enable the privileged Message Content intent. Ordinary posts are never forwarded; at most once per user every five minutes, the bot replies with instructions to use `/satphone` instead.

Use a **dedicated Discord application** for this bridge. Normal bridge startup is non-mutating, but the explicit one-time `register` action uses Discord's bulk guild-command registration API. It first reads the application's current guild commands and refuses to continue if anything other than the Satphone command exists, so it cannot silently erase another application's commands.

Discord application setup:

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create a dedicated application such as `Satphone Bridge`.
2. Open **Bot**, create/reset the bot token, and store it in Keychain as described below. Do not enable Message Content, Server Members, or Presence intents.
3. Leave **General Information → Interactions Endpoint URL** empty. Discord interaction delivery is either Gateway-based or HTTP-webhook-based, not both.
4. Under **Installation**, enable Guild Install with the `applications.commands` and `bot` scopes. The bot needs **View Channels** and **Send Messages** in the intended `satphone-v0` channel so it can warn that ordinary posts are not forwarded; it does not need Read Message History or Message Content. Then install it into the intended Discord server.
5. Put the server and channel IDs in `discord-bridge.json`. Prefer a nonempty `allowed_user_ids` list for local enforcement. An empty list is accepted only when `trust_discord_command_permissions` is explicitly `true`, which trusts the administrator-only Discord command configuration.

The checked-in `discord-bridge.example.json` documents the non-secret fields. This machine's real `discord-bridge.json` is ignored by Git and already contains the project, device, server, and channel identifiers. Tokens are never written there. Store the Discord bot token and an expiring Notehub Personal Access Token in macOS Keychain:

```bash
.venv/bin/python discord_bridge.py configure
.venv/bin/python discord_bridge.py check
```

Start the long-running bridge with:

```bash
.venv/bin/python discord_bridge.py register
.venv/bin/python discord_bridge.py
```

Run `register` once after installing the dedicated application, and again only when intentionally changing the slash-command definition. It refuses to overwrite unrelated guild commands. The `check` action verifies only local configuration, Keychain entries, and writable state storage; token validity, guild installation, and Notehub's required developer write permission are validated only by the live registration/connection and queue operation.

or open `Launch Discord Bridge.command` in Finder. The Mac must remain awake and the bridge must remain running for the slash command to work. It may run beside the SATPHONE serial tool because it never opens USB.

Each Discord interaction ID is claimed in `logs/discord-bridge.sqlite3` before Notehub is called. The database stores a message hash, not message text, and survives reconnects/restarts. Attempted queue operations are retained for 30 days; locally blocked IDs are retained for 20 minutes, just beyond Discord's 15-minute interaction-token lifetime, so a blocked interaction cannot become sendable later without letting denial traffic grow for a month. The bridge permits at most five accepted attempts per user and five total device attempts per minute, performs one Notehub request with no outer retry, and treats a timeout, malformed success response, or server error as uncertain. An identical message from the same user is suppressed for five minutes unless the user deliberately sets `allow_duplicate:true`. This is intentional because the Add QI Note endpoint has no caller-supplied idempotency key.

An HTTP success means only **queued in Notehub**. The device receives the Note on its next inbound sync—option 3 in the SATPHONE tool during development, or a separately approved periodic/continuous configuration. The Discord bridge never initiates a satellite sync and never claims delivery.

## Sync behavior

Inbound and outbound requests are always directional:

```json
{"req":"hub.sync","in":true}
```

```json
{"req":"hub.sync","out":true}
```

The state machine:

1. Verifies `hub.get` returned a ProductUID and a mode that accepts manual sync; unknown configuration and `off`/`dfu` fail closed.
2. Captures a passive `hub.sync.status` baseline and refuses to request when that query itself failed.
3. Refuses to issue a duplicate if structured status evidence suggests another sync is active.
4. Initiates at most one directional structured request.
5. Polls `hub.sync.status` without `sync:true`.
6. Requires correlated evidence of a new cycle or completion; stale status text, backwards epochs, and independently appearing age fields cannot produce false success.
7. Preserves the final raw response and distinguishes timeout, unresolved state, and confirmed API error.

The default local wait limit is 900 seconds because a Skylo attempt can take that long when location acquisition is needed. Override it for development:

```bash
.venv/bin/python satphone.py --sync-timeout 300 --poll-seconds 10
```

`hub.sync.status.completed` is an age in seconds, not a completion boolean. Likewise, `requested` is the age of the last explicit request, not a documented “active” boolean. The tool therefore labels active-sync detection as a conservative inference. It recognizes documented retry/wait states, `{sync}`, `{sync-end}`, and `{no-changes}`; `alert:true` is an error unless a documented status says a retry is currently active. `sync:true` means unsynchronized work remains and is never used by itself as proof that a sync is active or complete.

A local `{sync-end}` only means the Notecard-side sync cycle ended. Confirm actual end-to-end delivery and the real transport (`ntn:skylo`, `ntn:iridium`, or simulated `ntn:udp`) in the Notehub event.

## Incoming message safety

`View incoming messages → Read only` uses `note.changes` and does not acknowledge or delete Notes.

`Read and delete` shows the messages first, asks for explicit confirmation, re-reads that displayed queue prefix, then pops `messages.qi` with `note.get` and `delete:true`. Each returned Note is compared with the confirmed prefix, and any race or malformed response stops further deletion. An empty queue (`{note-noexist}` or `{notefile-noexist}`) is reported as a normal empty state.

Structured requests use `note-python`'s CRC/sequence-aware transaction replay. The application adds no outer retry around a destructive pop, so a final transport failure is reported conservatively as at most one uncertain deletion.

Checking for incoming satellite data performs one inbound sync and then reads locally without deletion.

## Location safety

`card.location` can contain GNSS, tower, triangulated, or stale last-known coordinates; fixed mode is verified separately with `card.location.mode`. The tool requires the exact documented `{gps}` success tag before calling coordinates GNSS-derived. Activity/error tags such as `{gps-starting}` or `{gps-inactive}` are not source proof.

Before setting a fixed test location, it reads and displays both:

```json
{"req":"card.location.mode"}
{"req":"ntn.gps"}
```

It then shows the exact proposed requests and asks for confirmation. For Skylo testing, fixed Notecard coordinates require both `card.location.mode` set to `fixed` and `ntn.gps` set to `on` so StarNote uses the Notecard location. Unknown or malformed current configuration blocks all writes. After a confirmed change, the tool re-reads mode, StarNote GPS source, and coordinates; it reports success only when the requested values persisted. Coordinates are never invented.

## Full diagnostic and reports

The diagnostic pass is read-only. It independently classifies:

- Notecard connectivity and API errors
- StarNote missing, idle, active, unknown-location, and unrecognized states
- NTN-capable versus non-NTN transport configuration
- current/last-known location and its likely source
- inbound and outbound template validity
- Notehub ProductUID and current connection state
- pending, completed, failed, or never-completed sync status
- zero versus one-or-more local incoming messages

Persistent repairs and an active satellite check are separate prompts. An empty inbox, an idle StarNote, and a disconnected periodic/minimum-mode Notecard are not treated as generic failures.

Exported reports are written to `logs/YYYY-MM-DD-HHMMSS-diagnostic.txt` and include the structured raw responses and transaction history. A collision-safe suffix prevents overwriting a report created in the same second. Review a report before sharing because it can contain device identifiers, precise location, and message contents.

## Raw debug mode

Raw debug mode uses the documented `card.trace` API, temporarily takes exclusive ownership of the USB stream, saves human-readable trace lines, and disables trace in a `finally` cleanup path. Normal application state never depends on parsing trace prose.

The user can monitor existing activity or explicitly request one inbound/outbound sync. The same strict `hub.get` and `hub.sync.status` preflight suppresses that request when configuration is unknown, the status query failed, or another sync appears active. Trace cleanup failure closes the serial connection and exits instead of allowing later structured requests on a contaminated stream. Logs are written to `logs/YYYY-MM-DD-HHMMSS-ntn-debug.txt`.

## Safety invariants

- No `card.restore`, factory reset, firmware flash, or `ntn.reset` path exists.
- No Notehub configuration is erased or overwritten.
- No message is deleted without selecting the delete action and confirming.
- No template, GPS mode, StarNote GPS source, or transport mode changes silently.
- Every persistent change reads and displays current state or a verified difference first.
- `note.add` never uses `sync:true`; the state machine is the only sync initiation path for normal structured actions.
- Raw debug is an explicit exception: after the shared strict preflight and a second confirmation, it may emit one response-less `cmd hub.sync` because trace mode exclusively owns the serial response stream.
- Status polling never uses `hub.sync.status` with `sync:true`.
- Low-level structured transaction replays retain `note-python`'s CRC/sequence identity; costly or destructive outer retries are not added.
- The Discord bridge never opens the serial port or initiates `hub.sync`; a Notehub timeout is marked uncertain and never automatically replayed.
- Discord and Notehub bearer credentials are read from environment variables or macOS Keychain and are never stored in configuration, logs, reports, or interaction history.

## Development and tests

The hardware-independent services are separated from the CLI:

```text
satphone.py
satphone/
├── cli.py
├── debug.py
├── diagnostics.py
├── discord_bridge.py
├── location.py
├── messages.py
├── models.py
├── notecard.py
├── reporting.py
└── satellite.py
tests/
```

Run all simulated tests:

```bash
.venv/bin/python -m unittest discover -v
```

The tests cover USB discovery, structured error recording, non-destructive reads, explicit deletion, template comparison, UTF-8 size limits, location handling, baseline-aware completion, directional sync, duplicate suppression, Discord authorization, durable interaction deduplication, the exact Notehub QI payload, and uncertain HTTP outcomes. Real satellite delivery still requires the physical Starter Kit and confirmation in Notehub.

## Official Blues references

- [StarNote quickstart](https://dev.blues.io/quickstart/starnote-quickstart/)
- [Satellite best practices](https://dev.blues.io/starnote/satellite-best-practices/)
- [`ntn.status` and `ntn.gps`](https://dev.blues.io/api-reference/notecard-api/ntn-requests/latest/)
- [`card.location`, `card.trace`, and `card.transport`](https://dev.blues.io/api-reference/notecard-api/card-requests/latest/)
- [`hub.sync` and `hub.sync.status`](https://dev.blues.io/api-reference/notecard-api/hub-requests/)
- [`note.add`, `note.get`, `note.changes`, and `note.template`](https://dev.blues.io/api-reference/notecard-api/note-requests/latest/)
- [Inbound Notes](https://dev.blues.io/notecard/notecard-walkthrough/inbound-requests-and-shared-data/)
- [Template data types](https://dev.blues.io/notecard/notecard-walkthrough/low-bandwidth-design/#understanding-template-data-types)
- [Notecard status/error codes](https://dev.blues.io/support/notecard-error-and-status-codes/)
- [Using trace mode](https://dev.blues.io/support/using-notecard-trace-mode/)
- [Notecard requests, responses, and commands](https://dev.blues.io/notecard/notecard-walkthrough/notecard-requests-and-responses/)
- [CRC and retry-aware requests](https://dev.blues.io/notecard/notecard-walkthrough/advanced-notecard-configuration/)
- [Python library setup for a desktop/SBC](https://dev.blues.io/tools-and-sdks/firmware-libraries/python-library/#pc-and-single-board-computer-use)
- [`note-python` v2.4.1](https://github.com/blues/note-python/tree/v2.4.1)
- [Notehub Add QI Note API](https://dev.blues.io/api-reference/notehub-api/device-api/#add-qi-note-notehub)
- [Notehub API authentication](https://dev.blues.io/api-reference/notehub-api/)
- [Discord application commands](https://docs.discord.com/developers/interactions/application-commands)
- [Discord Gateway interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)
