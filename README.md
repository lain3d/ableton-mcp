# AbletonMCP - Ableton Live Model Context Protocol Integration
[![smithery badge](https://smithery.ai/badge/@ahujasid/ableton-mcp)](https://smithery.ai/server/@ahujasid/ableton-mcp)

AbletonMCP connects Ableton Live to Claude AI through the Model Context Protocol (MCP), allowing Claude to directly interact with and control Ableton Live. This integration enables prompt-assisted music production, end-to-end track creation, and Live session and arrangement manipulation.

### Join the Community

Give feedback, get inspired, and build on top of the MCP: [Discord](https://discord.gg/3ZrMyGKnaU). Made by [Siddharth](https://x.com/sidahuj)

## Features

- **Two-way communication**: Connect Claude AI to Ableton Live through a socket-based server
- **Track manipulation**: Create, modify, and manipulate MIDI and audio tracks
- **Instrument and effect selection**: Claude can access and load the right instruments, effects and sounds from Ableton's library
- **Clip creation**: Create and edit MIDI clips with notes
- **Arrangement view composition**: Build full songs autonomously in Arrangement View, including sections like intro, buildup, drop, breakdown, and outro
- **Session control**: Start and stop playback, fire clips, and control transport across Session View and Arrangement View
- **Anonymous telemetry**: Usage tracking to help improve the tool (can be disabled)

## Components

The system consists of two main components:

1. **Ableton Remote Script** (`Ableton_Remote_Script/__init__.py`): A MIDI Remote Script for Ableton Live that creates a socket server to receive and execute commands
2. **MCP Server** (`server.py`): A Python server that implements the Model Context Protocol and connects to the Ableton Remote Script

## Installation

### Installing via Smithery

To install Ableton Live Integration for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@ahujasid/ableton-mcp):

```bash
npx -y @smithery/cli install @ahujasid/ableton-mcp --client claude
```

### Prerequisites

- Ableton Live 10 or newer
- Python 3.8 or newer
- [uv package manager](https://astral.sh/uv)

If you're on Mac, please install uv as:
```
brew install uv
```

Otherwise, install from [uv's official website][https://docs.astral.sh/uv/getting-started/installation/]

⚠️ Do not proceed before installing UV

### Claude for Desktop Integration

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

1. Go to Claude > Settings > Developer > Edit Config > claude_desktop_config.json to include the following:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": [
                "ableton-mcp"
            ]
        }
    }
}
```

### Cursor Integration

Run ableton-mcp without installing it permanently through uvx. Go to Cursor Settings > MCP and paste this as a command:

```
uvx ableton-mcp
```

⚠️ Only run one instance of the MCP server (either on Cursor or Claude Desktop), not both

### Installing the Ableton Remote Script

[Follow along with the setup instructions video](https://youtu.be/iJWJqyVuPS8)

1. Download the `AbletonMCP_Remote_Script/__init__.py` file from this repo

2. Copy the folder to Ableton's MIDI Remote Scripts directory. Different OS and versions have different locations. **One of these should work, you might have to look**:

   **For macOS:**
   - Method 1: Go to Applications > Right-click on Ableton Live app → Show Package Contents → Navigate to:
     `Contents/App-Resources/MIDI Remote Scripts/`
   - Method 2: If it's not there in the first method, use the direct path (replace XX with your version number):
     `/Users/[Username]/Library/Preferences/Ableton/Live XX/User Remote Scripts`
   
   **For Windows:**
   - Method 1:
     C:\Users\[Username]\AppData\Roaming\Ableton\Live x.x.x\Preferences\User Remote Scripts 
   - Method 2:
     `C:\ProgramData\Ableton\Live XX\Resources\MIDI Remote Scripts\`
   - Method 3:
     `C:\Program Files\Ableton\Live XX\Resources\MIDI Remote Scripts\`
   *Note: Replace XX with your Ableton version number (e.g., 10, 11, 12)*

4. Create a folder called 'AbletonMCP' in the Remote Scripts directory and paste the downloaded '\_\_init\_\_.py' file

3. Launch Ableton Live

4. Go to Settings/Preferences → Link, Tempo & MIDI

5. In the Control Surface dropdown, select "AbletonMCP"

6. Set Input and Output to "None"

## Usage

### Starting the Connection

1. Ensure the Ableton Remote Script is loaded in Ableton Live
2. Make sure the MCP server is configured in Claude Desktop or Cursor
3. The connection should be established automatically when you interact with Claude

### Using with Claude

Once the config file has been set on Claude, and the remote script is running in Ableton, you will see a hammer icon with tools for the Ableton MCP.

## Capabilities

This fork substantially extends the original with mixer, device, automation,
and workflow control (60 tools in total). Grouped by area:

**Session & tracks**
- Get session and track information
- Create MIDI, audio, and return tracks; rename, duplicate, and delete tracks
- Mixer control: volume, pan, sends, mute, solo, arm (regular / return / master)
- Input & output routing (incl. routing one track into another / resampling)

**Clips & MIDI**
- Create and trigger MIDI and audio clips; import audio files as clips
- Add, read back, and remove MIDI notes — including per-note expression
  (probability, velocity deviation, release velocity)
- Quantize clips; set loop points; set clip gain / pitch / warp (audio)
- Duplicate and delete clips

**Devices**
- Load instruments and effects from Ableton's browser
- List and set device parameters (by index or name), including devices nested
  inside racks (Instrument / Audio Effect / Drum Racks)
- Enable/disable and delete devices; inspect rack chains

**Automation**
- Write clip automation envelopes for any device parameter (e.g. filter sweeps)
- Write clip automation for mixer volume / pan / sends

**Scenes, transport & workflow**
- Create, duplicate, delete, rename, and fire scenes
- Playback/transport control; move the arrangement playhead; build arrangements
- Undo / redo / capture MIDI; change tempo and time signature
- Metronome, arrangement/session record, arrangement loop region
- Colors (track / clip / scene); fold group tracks; select tracks/scenes and
  switch Live's views
- `batch` — run many operations in a single round-trip (~13× faster than
  issuing them one at a time); the whole batch is a single undo step

**Reacting to the user (state observers)**
- `subscribe` to Live changes and drain them with `poll_events` — so the
  assistant can respond to what you do in Live, while the connection stays
  request/response. Targets: transport, selection, track/scene add-remove,
  detail clip, per-track mixer (`track:<index>`), and session clip play/queue
  state (`playing_slots`)

## Example Commands

Here are some examples of what you can ask Claude to do:

- "Create an 80s synthwave track" [Demo](https://youtu.be/VH9g66e42XA)
- "Create a Metro Boomin style hip-hop beat"
- "Create a full arrangement with an intro, buildup, drop, breakdown, and outro"
- "Create a new MIDI track with a synth bass instrument"
- "Add reverb to my drums"
- "Create a 4-bar MIDI clip with a simple melody"
- "Get information about the current Ableton session"
- "Load a 808 drum rack into the selected track"
- "Add a jazz chord progression to the clip in track 1"
- "Set the tempo to 120 BPM"
- "Play the clip in track 2"


## Troubleshooting

- **Connection issues**: Make sure the Ableton Remote Script is loaded, and the MCP server is configured on Claude
- **Timeout errors**: Try simplifying your requests or breaking them into smaller steps
- **Have you tried turning it off and on again?**: If you're still having connection errors, try restarting both Claude and Ableton Live

## Technical Details

### Communication Protocol

The system uses a simple JSON-based protocol over TCP sockets:

- Commands are sent as JSON objects with a `type` and optional `params`
- Responses are JSON objects with a `status` and `result` or `message`

### Security

- The command server binds to `127.0.0.1` (loopback only), so it is not
  reachable from the network. Run only one MCP client against it at a time.
- The shared client socket is serialized with a lock, so concurrent tool calls
  won't corrupt each other.

### Limitations

Some things are **not possible through Ableton's Remote Script API** and won't
be added here:

- **No saving the Live Set** — the API exposes no save function. Save your work
  yourself before extensive experimentation.
- **No audio rendering / export / bounce**, and no freeze/flatten.
- **No raw audio generation or analysis** — you supply audio files; the tool
  places, warps, and mixes them, but can't synthesize or read sample data.
- **Automation is stepped, not smooth** — envelopes are written as flat
  segments (a staircase), not linear/curved ramps.
- **No MIDI CC envelopes in clips** — clip envelopes are keyed to device
  parameters; raw MIDI CC lanes (mod wheel, etc.) aren't exposed.
- **No tempo automation** — the Song Tempo parameter lives on the master track,
  and the LOM rejects automating another track's parameter from a clip
  (`parameter belongs to another track`); there is no arrangement-lane
  automation API for song-level parameters either.
- **Arrangement editing is limited** — you can push Session clips to the
  Arrangement and move the playhead, but not move/resize/delete arrangement
  clips or edit arrangement automation.
- **No warp-marker editing** (only warp on/off + mode) and **no device
  reordering** within a chain.
- **Can't create group tracks** — the API can fold/unfold existing groups but
  has no call to group tracks in the first place.

### Roadmap (feasible next steps)

These are supported by the API and are good candidates for future work:

- Overdub and punch in/out
- Groove pool, crossfader assignment, and finer send/return routing
- Simpler/Sampler sample loading by path

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Telemetry

AbletonMCP collects anonymous usage data to help improve the tool. This includes:
- Tool usage statistics (which features are used)
- Session information (for daily/monthly active user counts)
- Error rates and performance metrics

No personal information, project names, or audio content is collected.

### Opting Out

To disable telemetry, set one of these environment variables before starting the MCP server:

```bash
export ABLETON_MCP_DISABLE_TELEMETRY=true
```

Or use any of these alternatives:
- `DISABLE_TELEMETRY=true`
- `MCP_DISABLE_TELEMETRY=true`

For Claude Desktop, add the environment variable to your config:

```json
{
    "mcpServers": {
        "AbletonMCP": {
            "command": "uvx",
            "args": ["ableton-mcp"],
            "env": {
                "ABLETON_MCP_DISABLE_TELEMETRY": "true"
            }
        }
    }
}
```

## Disclaimer

This is a third-party integration and not made by Ableton.
