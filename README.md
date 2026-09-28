# Recorder

A native macOS meeting recorder that works the same whether the call is on
Meet, Zoom, Teams, or a phone bridge. Hit Record before the call and Stop after.
You get a local transcript, a short summary, decisions, and an action list
split into what you owe, what you're waiting on, and FYI.

Audio never leaves the laptop. Transcription runs locally with `parakeet-mlx`
on Apple Silicon, with Hugging Face network access switched off at runtime.
Text goes to Claude on AWS Bedrock in your own account, and only in three
places: the transcript after each meeting (one short call), meeting prep
(calendar details, email snippets and prior notes for that meeting), and Ask
(the index of every meeting plus the full text of the most relevant ones).

Write-up: [I needed one place for every meeting. So I built a recorder in chat.](https://nvg8.ai/writing/meeting-recorder)

## What's in it

- **Recording.** ScreenCaptureKit captures system audio (the other people) and
  the microphone (you) as two 48 kHz WAV tracks. Two tracks mean the transcript
  is speaker-labeled `You:` / `Remote:`, so owner attribution is a read, not a
  guess.
- **Transcription.** `parakeet-mlx`, chunked at 120 s with overlap so long
  meetings fit the Metal memory budget.
- **Extraction.** Summary, decisions, and todos with owner, due date and the
  quote that justified each one. Open items from earlier meetings in a
  recurring series are carried forward and closed when they're done.
- **Calendar matching.** Each recording is matched to the calendar event it
  overlaps, so owners are real attendee names.
- **Meeting prep** (optional). A panel for upcoming meetings that pulls prior
  transcripts, recent email (Gmail API or a local notmuch index), and Gemini
  meeting notes, and has Claude write a short brief.
- **Ask.** Ask a question across every recorded meeting.

## Layout

```
Package.swift                SwiftPM exec target
Sources/Recorder/            SwiftUI app: AppModel (state), ContentView + Components (UI),
                             SystemAudioRecorder, PipelineRunner (runs the sidecar)
Resources/                   Info.plist (TCC strings), ad-hoc signing entitlements
scripts/build.sh             assemble Recorder.app + codesign
python/                      uv-managed sidecar, invoked from Swift by subprocess
  recorder_config.py         identity, accounts, and data paths (read this first)
  config.example.json        copy to ~/.config/recorder/config.json
  transcribe.py              parakeet-mlx, mixes the two tracks
  extract_todos.py           Bedrock converse() call
  fetch_meeting.py           recording → calendar event
  fetch_meeting_prep.py      prep panel data
  prep_synthesis.py          Bedrock brief writing for the prep panel
  ask_meetings.py            question answering across the archive
  import_gemini_notes.py     import Gemini "Notes" emails as meetings
  import_meet_notes.py       import Meet notes docs attached to events
  google_auth.py             one registry for every Google OAuth token
  _prep/                     vendored prep helpers (email search, briefs)
```

## Prereqs

- macOS 14+ on Apple Silicon
- Xcode Command Line Tools (`xcode-select --install`)
- [`uv`](https://docs.astral.sh/uv/)
- AWS credentials with Bedrock access to Claude in your region

## Setup

```sh
cd python && uv sync && cd ..
./scripts/build.sh
open build/Recorder.app
```

The first transcription downloads `mlx-community/parakeet-tdt-0.6b-v2` from
Hugging Face (about 600 MB). Set `HF_TOKEN` to avoid rate limits.

On first launch macOS asks for **Screen Recording** permission, which it
requires even for audio-only capture, and **Microphone**. Grant both in
System Settings → Privacy & Security, then relaunch.

## Configuration

Recording, transcription, and todo extraction work with no config. Tell it who
you are so "mine" todos land on you:

```sh
mkdir -p ~/.config/recorder
cp python/config.example.json ~/.config/recorder/config.json
# edit user_name, user_email, accounts
```

An **account** is one work identity: a mailbox plus the calendars it owns. List
each one if you work across several companies. `match_calendars` is the set
searched when matching a recording to a meeting. Leave personal and family
calendars out; their long blocks cause false matches.

Google features (calendar matching, prep, imports) need a Desktop OAuth client
from Google Cloud Console saved as
`~/.config/recorder/config/google_oauth_client.json`. Then:

```sh
cd python
uv run python google_auth.py status          # what's alive, what's missing
uv run python google_auth.py reauth --all    # authorize
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RECORDER_CONFIG` | `~/.config/recorder/config.json` | Config file |
| `RECORDER_DATA_ROOT` | `~/.config/recorder` | Tokens, `.env`, daily briefs |
| `RECORDER_USER_NAME` / `RECORDER_USER_EMAIL` | from config | Who "you" are |
| `RECORDER_ACCOUNT` | `default_account` | Account the prep panel reads |
| `BEDROCK_REGION` | `us-east-1` | |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Extraction model |
| `EMAIL_SEARCH_BACKEND[_<ACCOUNT>]` | `notmuch` | `gmail` or `notmuch` for prep email |
| `RECORDER_PYTHON_DIR` | bundled | Run the sidecar from source during dev |

The `.app` inherits the environment of whatever launches it. Variables can also
go in `~/.config/recorder/.env`.

### Daily briefs

If another tool already writes meeting briefs, point it at
`<data_root>/briefs/YYYY-MM-DD/HHMM_<slug>.md` with `## Why this meeting
matters`, `## Background`, `## Talking points`, `## Open questions` and
`## Suggested ask` sections. The prep panel prefers those over its own.

## Output

Everything is plain files in `~/Library/Application Support/Recorder/recordings/`,
one stem per recording (`rec-YYYYMMDD-HHMMSS`):

- `.system.wav`, `.mic.wav`: the two tracks
- `.transcript.json`: `{text, segments[]}`
- `.event.json`: the matched calendar event
- `.todos.json`: `{summary, todos[], decisions[], resolved_prior[]}`

The filesystem is the source of truth. There is no database.

## Development

```sh
swift run -c release    # no .app, so no Screen Recording prompt; Terminal needs the permission
export RECORDER_PYTHON_DIR="$PWD/python"
cd python && uv run python -m unittest
```

## Security notes

- OAuth tokens are written `0600` under `~/.config/recorder/config/`. Every
  Google scope requested is read-only (Calendar, Gmail, Drive, Docs).
- Calendar invites, email and transcripts are untrusted input: anyone can send
  you an invite. They only ever reach the model as text, the model's output is
  only ever displayed, and links from calendar data open only if they are
  `http(s)`. Nothing the model says is executed.
- Meeting titles, Ask questions and sidecar output are logged as private in the
  macOS unified log.

## Known limits

- Screen Recording permission is required even for audio-only capture.
- Transcription runs after the call, not streaming.
- Apple Silicon only (MLX).

## License

MIT. See [LICENSE](LICENSE).
