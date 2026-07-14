# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None
    # Serializes access to the shared socket so concurrent tool calls (FastMCP
    # runs sync tools in a thread pool) don't interleave request/response frames.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Ableton: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192, timeout=15.0):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(timeout)  # Wider for slow operations (e.g. audio import, batch)
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "create_return_track",
            "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "set_tempo", "fire_clip", "stop_clip",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Mixer, device parameters, track management
            "set_track_volume", "set_track_pan", "set_track_send",
            "set_track_mute", "set_track_solo", "set_track_arm",
            "delete_track", "duplicate_track", "set_device_parameter",
            "delete_device", "set_device_on",
            # Clip content: notes, quantize, loop & audio warp
            "remove_clip_notes", "quantize_clip", "set_clip_loop",
            "set_clip_gain", "set_clip_pitch", "set_clip_warp",
            # Scenes, clip management, routing, automation, transport
            "create_scene", "delete_scene", "duplicate_scene", "fire_scene", "set_scene_name",
            "delete_clip", "duplicate_clip",
            "set_track_input_routing", "set_track_output_routing",
            "set_clip_envelope", "set_clip_mixer_envelope", "clear_clip_envelope",
            "undo", "redo", "capture_midi",
            # Colors, recording/transport, selection & view
            "set_track_color", "set_clip_color", "set_scene_color",
            "set_metronome", "set_arrangement_record", "set_session_record",
            "set_arrangement_loop", "set_time_signature", "set_track_fold",
            "select_track", "select_scene", "show_view",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement"
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}

        # Hold the lock across the whole send/receive so concurrent tool calls
        # don't interleave request/response frames on the shared socket.
        self._lock.acquire()
        try:
            # Another thread may have invalidated the socket (on error) between
            # the check above and acquiring the lock.
            if self.sock is None and not self.connect():
                raise ConnectionError("Not connected to Ableton")

            logger.info(f"Sending command: {command_type} with params: {params}")

            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type. A batch scales with the work it
            # queues (plus any audio-import budgets inside it).
            if command_type == "batch":
                ops = (params or {}).get("operations", [])
                timeout = max(60.0, 2.0 * len(ops) + sum(
                    long_running_commands.get(o.get("type", ""), 0.0) for o in ops))
            elif command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock, timeout=timeout)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")
        finally:
            self._lock.release()

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        yield {}
    finally:
        global _ableton_connection
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data. Hold the
            # connection lock so this doesn't toggle blocking mode / read the
            # socket while another thread is mid send_command on it.
            # MSG_PEEK raises BlockingIOError if alive but no data waiting,
            # or returns b'' if the remote end has closed the connection.
            with _ableton_connection._lock:
                _ableton_connection.sock.setblocking(False)
                try:
                    data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                    if data == b'':
                        raise ConnectionError("Remote end closed")
                except BlockingIOError:
                    pass  # Socket is alive, just no data waiting — this is normal
                finally:
                    if _ableton_connection.sock is not None:
                        _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
@telemetry_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"

@mcp.tool()
@telemetry_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dicts. Required: pitch, start_time, duration, velocity.
      Optional: mute (bool), plus per-note expression (Live 11+):
        probability (0.0-1.0, chance the note plays),
        velocity_deviation (added random velocity range),
        release_velocity (0-127).
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"

@mcp.tool()
@telemetry_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


# ── Audio tracks, mixer & device parameters ───────────────────────────────────

@mcp.tool()
@telemetry_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created audio track '{result.get('name', '?')}' at index {result.get('index', '?')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_return_track")
def create_return_track(ctx: Context, user_prompt: str = "") -> str:
    """
    Create a new return track (for sends/aux effects). Appended after existing returns.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_return_track", {})
        return f"Created return track '{result.get('name', '?')}' at return index {result.get('index', '?')}"
    except Exception as e:
        logger.error(f"Error creating return track: {str(e)}")
        return f"Error creating return track: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_volume")
def set_track_volume(ctx: Context, track_index: int, value: float, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Set a track's volume fader.

    Parameters:
    - track_index: Index of the track (ignored when track_type is 'master')
    - value: Normalized volume 0.0-1.0, where ~0.85 = 0 dB and 1.0 = +6 dB
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_volume", {"track_index": track_index, "value": value, "track_type": track_type})
        return f"Set volume of '{result.get('name', track_index)}' to {result.get('volume', value):.3f}"
    except Exception as e:
        logger.error(f"Error setting track volume: {str(e)}")
        return f"Error setting track volume: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_pan")
def set_track_pan(ctx: Context, track_index: int, value: float, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Set a track's pan position.

    Parameters:
    - track_index: Index of the track (ignored when track_type is 'master')
    - value: -1.0 (hard left) .. 0.0 (center) .. 1.0 (hard right)
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_pan", {"track_index": track_index, "value": value, "track_type": track_type})
        return f"Set pan of '{result.get('name', track_index)}' to {result.get('panning', value):.3f}"
    except Exception as e:
        logger.error(f"Error setting track pan: {str(e)}")
        return f"Error setting track pan: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_send")
def set_track_send(ctx: Context, track_index: int, send_index: int, value: float, user_prompt: str = "") -> str:
    """
    Set a track's send level to a return track.

    Parameters:
    - track_index: Index of the (regular) track
    - send_index: Index of the send (0 = send A, 1 = send B, ...)
    - value: Normalized send amount 0.0-1.0
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_send", {"track_index": track_index, "send_index": send_index, "value": value})
        return f"Set send {send_index} of '{result.get('name', track_index)}' to {result.get('value', value):.3f}"
    except Exception as e:
        logger.error(f"Error setting track send: {str(e)}")
        return f"Error setting track send: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_mute")
def set_track_mute(ctx: Context, track_index: int, mute: bool = True, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Mute or unmute a track.

    Parameters:
    - track_index: Index of the track
    - mute: True to mute, False to unmute
    - track_type: 'regular' (default) or 'return'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_mute", {"track_index": track_index, "mute": mute, "track_type": track_type})
        return f"Set mute of '{result.get('name', track_index)}' to {result.get('mute', mute)}"
    except Exception as e:
        logger.error(f"Error setting track mute: {str(e)}")
        return f"Error setting track mute: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_solo")
def set_track_solo(ctx: Context, track_index: int, solo: bool = True, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Solo or unsolo a track.

    Parameters:
    - track_index: Index of the track
    - solo: True to solo, False to unsolo
    - track_type: 'regular' (default) or 'return'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_solo", {"track_index": track_index, "solo": solo, "track_type": track_type})
        return f"Set solo of '{result.get('name', track_index)}' to {result.get('solo', solo)}"
    except Exception as e:
        logger.error(f"Error setting track solo: {str(e)}")
        return f"Error setting track solo: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_arm")
def set_track_arm(ctx: Context, track_index: int, arm: bool = True, user_prompt: str = "") -> str:
    """
    Arm or disarm a track for recording.

    Parameters:
    - track_index: Index of the track
    - arm: True to arm, False to disarm
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_arm", {"track_index": track_index, "arm": arm})
        return f"Set arm of '{result.get('name', track_index)}' to {result.get('arm', arm)}"
    except Exception as e:
        logger.error(f"Error setting track arm: {str(e)}")
        return f"Error setting track arm: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_track")
def delete_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Delete a track from the session.

    Parameters:
    - track_index: Index of the track to delete
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_track", {"track_index": track_index})
        return f"Deleted track '{result.get('deleted_name', track_index)}' ({result.get('track_count', '?')} tracks remain)"
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"


@mcp.tool()
@telemetry_tool("duplicate_track")
def duplicate_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Duplicate a track (including its clips and devices). The copy is inserted right after it.

    Parameters:
    - track_index: Index of the track to duplicate
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("duplicate_track", {"track_index": track_index})
        return f"Duplicated track to '{result.get('name', '?')}' at index {result.get('index', '?')}"
    except Exception as e:
        logger.error(f"Error duplicating track: {str(e)}")
        return f"Error duplicating track: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
def get_device_parameters(ctx: Context, track_index: int, device_index: int, track_type: str = "regular", chain_index: int = None, chain_device_index: int = None, user_prompt: str = "") -> str:
    """
    List all parameters of a device on a track, with current value, range, and display value.

    Use this before set_device_parameter to discover parameter names/indices and valid ranges.
    To reach a device nested inside a rack, pass chain_index (+ chain_device_index) — see
    get_device_chains to discover the rack's structure.

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the device in the track's device chain (0 = first)
    - track_type: 'regular' (default), 'return', or 'master'
    - chain_index: If the device is a rack, index of the chain to descend into (optional)
    - chain_device_index: Index of the device within that chain (default 0, optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index, "track_type": track_type}
        if chain_index is not None:
            params["chain_index"] = chain_index
        if chain_device_index is not None:
            params["chain_device_index"] = chain_device_index
        result = ableton.send_command("get_device_parameters", params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@telemetry_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    value: float,
    parameter_index: int = None,
    parameter_name: str = None,
    track_type: str = "regular",
    chain_index: int = None,
    chain_device_index: int = None,
    user_prompt: str = ""
) -> str:
    """
    Set a device parameter (e.g. a filter cutoff, compressor threshold, reverb size).

    Provide either parameter_index or parameter_name to identify the knob. The value is
    clamped to the parameter's valid range. Call get_device_parameters first to see options.
    For a device nested inside a rack, pass chain_index (+ chain_device_index).

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the device in the track's device chain (0 = first)
    - value: The new value (in the parameter's own units/range)
    - parameter_index: Index of the parameter to set (optional if parameter_name given)
    - parameter_name: Name of the parameter to set, case-insensitive (optional if parameter_index given)
    - track_type: 'regular' (default), 'return', or 'master'
    - chain_index: If the device is a rack, index of the chain to descend into (optional)
    - chain_device_index: Index of the device within that chain (default 0, optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index, "value": value, "track_type": track_type}
        if parameter_index is not None:
            params["parameter_index"] = parameter_index
        if parameter_name is not None:
            params["parameter_name"] = parameter_name
        if chain_index is not None:
            params["chain_index"] = chain_index
        if chain_device_index is not None:
            params["chain_device_index"] = chain_device_index
        result = ableton.send_command("set_device_parameter", params)
        disp = result.get("display_value")
        disp_str = f" ({disp})" if disp else ""
        return f"Set '{result.get('parameter_name', '?')}' on '{result.get('device_name', '?')}' to {result.get('value', value)}{disp_str}"
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


# ── Clip content: notes, quantize, loop & audio warp ──────────────────────────

@mcp.tool()
@telemetry_tool("get_clip_notes")
def get_clip_notes(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Read the MIDI notes in a clip so you can inspect or iterate on them.

    Returns each note's pitch, start_time (beats), duration (beats), velocity, and mute.

    Parameters:
    - track_index: Index of the track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_clip_notes", {"track_index": track_index, "clip_index": clip_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("remove_clip_notes")
def remove_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    from_time: float = 0.0,
    from_pitch: int = 0,
    time_span: float = 1000000.0,
    pitch_span: int = 128,
    user_prompt: str = ""
) -> str:
    """
    Remove MIDI notes from a clip. With defaults, clears the whole clip; narrow the
    window with from_time/time_span (beats) and from_pitch/pitch_span (MIDI note numbers).

    Combine with add_notes_to_clip to replace a clip's contents.

    Parameters:
    - track_index: Index of the track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - from_time: Start of the time window in beats (default 0.0)
    - from_pitch: Lowest MIDI pitch to remove (default 0)
    - time_span: Length of the time window in beats (default very large = to end)
    - pitch_span: Number of pitches to span from from_pitch (default 128 = all)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("remove_clip_notes", {
            "track_index": track_index, "clip_index": clip_index,
            "from_time": from_time, "from_pitch": from_pitch,
            "time_span": time_span, "pitch_span": pitch_span
        })
        return f"Removed notes from '{result.get('clip_name', clip_index)}' ({result.get('remaining_note_count', '?')} notes remain)"
    except Exception as e:
        logger.error(f"Error removing clip notes: {str(e)}")
        return f"Error removing clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("quantize_clip")
def quantize_clip(ctx: Context, track_index: int, clip_index: int, grid: str = "1/16", amount: float = 1.0, user_prompt: str = "") -> str:
    """
    Quantize a clip's notes to a grid.

    Parameters:
    - track_index: Index of the track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - grid: Grid resolution: '1/4', '1/4t', '1/8', '1/8t', '1/16', '1/16t', '1/32'
            (the 't' variants are triplets)
    - amount: Quantize strength 0.0-1.0 (1.0 = snap fully to grid)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("quantize_clip", {
            "track_index": track_index, "clip_index": clip_index, "grid": grid, "amount": amount
        })
        return f"Quantized '{result.get('clip_name', clip_index)}' to {grid} at {result.get('amount', amount)} strength"
    except Exception as e:
        logger.error(f"Error quantizing clip: {str(e)}")
        return f"Error quantizing clip: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_loop")
def set_clip_loop(
    ctx: Context,
    track_index: int,
    clip_index: int,
    looping: bool = True,
    loop_start: float = None,
    loop_end: float = None,
    user_prompt: str = ""
) -> str:
    """
    Turn a clip's loop on/off and optionally set its loop boundaries (in beats).

    Parameters:
    - track_index: Index of the track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - looping: True to loop, False to play once
    - loop_start: Loop start position in beats (optional)
    - loop_end: Loop end position in beats (optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_loop", {
            "track_index": track_index, "clip_index": clip_index,
            "looping": looping, "loop_start": loop_start, "loop_end": loop_end
        })
        return f"Set loop on '{result.get('clip_name', clip_index)}': looping={result.get('looping')} start={result.get('loop_start')} end={result.get('loop_end')}"
    except Exception as e:
        logger.error(f"Error setting clip loop: {str(e)}")
        return f"Error setting clip loop: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_gain")
def set_clip_gain(ctx: Context, track_index: int, clip_index: int, gain: float, user_prompt: str = "") -> str:
    """
    Set an audio clip's gain (normalized 0.0-1.0).

    Parameters:
    - track_index: Index of the audio track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - gain: Normalized gain 0.0-1.0
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_gain", {"track_index": track_index, "clip_index": clip_index, "gain": gain})
        disp = result.get("gain_display")
        disp_str = f" ({disp})" if disp else ""
        return f"Set gain on '{result.get('clip_name', clip_index)}' to {result.get('gain', gain):.3f}{disp_str}"
    except Exception as e:
        logger.error(f"Error setting clip gain: {str(e)}")
        return f"Error setting clip gain: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_pitch")
def set_clip_pitch(ctx: Context, track_index: int, clip_index: int, coarse: int = 0, fine: int = 0, user_prompt: str = "") -> str:
    """
    Transpose an audio clip.

    Parameters:
    - track_index: Index of the audio track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - coarse: Transpose in semitones (-48 to 48)
    - fine: Fine transpose in cents (-50 to 50)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_pitch", {"track_index": track_index, "clip_index": clip_index, "coarse": coarse, "fine": fine})
        return f"Set pitch on '{result.get('clip_name', clip_index)}' to {result.get('pitch_coarse', coarse)} st, {result.get('pitch_fine', fine)} cents"
    except Exception as e:
        logger.error(f"Error setting clip pitch: {str(e)}")
        return f"Error setting clip pitch: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_warp")
def set_clip_warp(ctx: Context, track_index: int, clip_index: int, warping: bool = True, warp_mode: int = None, user_prompt: str = "") -> str:
    """
    Toggle warping on an audio clip and optionally set the warp mode.

    Parameters:
    - track_index: Index of the audio track containing the clip
    - clip_index: Index of the clip slot containing the clip
    - warping: True to warp (tempo-follow), False to play at original speed
    - warp_mode: Warp algorithm index (0=Beats, 1=Tones, 2=Texture, 3=Re-Pitch, 4=Complex, 6=Complex Pro), optional
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_warp", {"track_index": track_index, "clip_index": clip_index, "warping": warping, "warp_mode": warp_mode})
        return f"Set warp on '{result.get('clip_name', clip_index)}': warping={result.get('warping')} mode={result.get('warp_mode')}"
    except Exception as e:
        logger.error(f"Error setting clip warp: {str(e)}")
        return f"Error setting clip warp: {str(e)}"


# ── Scenes ─────────────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("create_scene")
def create_scene(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new scene. index=-1 appends at the end.

    Parameters:
    - index: The index to insert the scene at (-1 = end)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_scene", {"index": index})
        return f"Created scene '{result.get('name', '?')}' at index {result.get('index', '?')}"
    except Exception as e:
        logger.error(f"Error creating scene: {str(e)}")
        return f"Error creating scene: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_scene")
def delete_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Delete a scene.

    Parameters:
    - scene_index: The index of the scene to delete
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_scene", {"scene_index": scene_index})
        return f"Deleted scene {scene_index} ({result.get('scene_count', '?')} scenes remain)"
    except Exception as e:
        logger.error(f"Error deleting scene: {str(e)}")
        return f"Error deleting scene: {str(e)}"


@mcp.tool()
@telemetry_tool("duplicate_scene")
def duplicate_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Duplicate a scene (the copy is inserted right after it).

    Parameters:
    - scene_index: The index of the scene to duplicate
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("duplicate_scene", {"scene_index": scene_index})
        return f"Duplicated scene to index {result.get('index', '?')}"
    except Exception as e:
        logger.error(f"Error duplicating scene: {str(e)}")
        return f"Error duplicating scene: {str(e)}"


@mcp.tool()
@telemetry_tool("fire_scene")
def fire_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Launch a scene (fires every clip in that scene row).

    Parameters:
    - scene_index: The index of the scene to fire
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("fire_scene", {"scene_index": scene_index})
        return f"Fired scene {scene_index}"
    except Exception as e:
        logger.error(f"Error firing scene: {str(e)}")
        return f"Error firing scene: {str(e)}"


@mcp.tool()
@telemetry_tool("set_scene_name")
def set_scene_name(ctx: Context, scene_index: int, name: str, user_prompt: str = "") -> str:
    """
    Rename a scene.

    Parameters:
    - scene_index: The index of the scene to rename
    - name: The new scene name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_scene_name", {"scene_index": scene_index, "name": name})
        return f"Renamed scene {scene_index} to '{result.get('name', name)}'"
    except Exception as e:
        logger.error(f"Error setting scene name: {str(e)}")
        return f"Error setting scene name: {str(e)}"


# ── Clip management ────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete a clip from a Session slot.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {"track_index": track_index, "clip_index": clip_index})
        return f"Deleted clip '{result.get('deleted_name', clip_index)}' from track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("duplicate_clip")
def duplicate_clip(ctx: Context, track_index: int, clip_index: int, target_clip_index: int = None, user_prompt: str = "") -> str:
    """
    Duplicate a Session clip to another slot on the same track.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the source clip slot
    - target_clip_index: The destination slot (optional; defaults to the next empty slot)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "clip_index": clip_index}
        if target_clip_index is not None:
            params["target_clip_index"] = target_clip_index
        result = ableton.send_command("duplicate_clip", params)
        return f"Duplicated clip from slot {clip_index} to slot {result.get('target_index', '?')} on track {track_index}"
    except Exception as e:
        logger.error(f"Error duplicating clip: {str(e)}")
        return f"Error duplicating clip: {str(e)}"


# ── Track routing ──────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_track_routing")
def get_track_routing(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get a track's current input/output routing and the available options.

    Use this to discover valid routing names (e.g. to resample or route one track
    into another) before calling set_track_input_routing / set_track_output_routing.

    Parameters:
    - track_index: The index of the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_routing", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track routing: {str(e)}")
        return f"Error getting track routing: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_input_routing")
def set_track_input_routing(ctx: Context, track_index: int, routing_name: str = None, channel: str = None, user_prompt: str = "") -> str:
    """
    Set a track's input routing (source). Names must match get_track_routing output.

    Parameters:
    - track_index: The index of the track
    - routing_name: Display name of the input routing type (optional)
    - channel: Display name of the input routing channel (optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_input_routing", {"track_index": track_index, "routing_name": routing_name, "channel": channel})
        return f"Set input routing of '{result.get('track_name', track_index)}' to {result.get('input_routing_type')} / {result.get('input_routing_channel')}"
    except Exception as e:
        logger.error(f"Error setting input routing: {str(e)}")
        return f"Error setting input routing: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_output_routing")
def set_track_output_routing(ctx: Context, track_index: int, routing_name: str = None, channel: str = None, user_prompt: str = "") -> str:
    """
    Set a track's output routing (destination). Names must match get_track_routing output.

    Parameters:
    - track_index: The index of the track
    - routing_name: Display name of the output routing type (optional)
    - channel: Display name of the output routing channel (optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_output_routing", {"track_index": track_index, "routing_name": routing_name, "channel": channel})
        return f"Set output routing of '{result.get('track_name', track_index)}' to {result.get('output_routing_type')} / {result.get('output_routing_channel')}"
    except Exception as e:
        logger.error(f"Error setting output routing: {str(e)}")
        return f"Error setting output routing: {str(e)}"


# ── Clip automation envelopes ──────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("set_clip_envelope")
def set_clip_envelope(
    ctx: Context,
    track_index: int,
    clip_index: int,
    device_index: int,
    points: List[Dict[str, float]],
    parameter_index: int = None,
    parameter_name: str = None,
    clear_existing: bool = True,
    chain_index: int = None,
    chain_device_index: int = None,
    user_prompt: str = ""
) -> str:
    """
    Write automation for a device parameter inside a clip (e.g. sweep a filter cutoff).

    The parameter must belong to a device on the same track as the clip. Identify it by
    parameter_index or parameter_name (see get_device_parameters). Envelopes are written
    as stepped segments: each point holds until the next. For a nested rack device, pass
    chain_index (+ chain_device_index). To automate volume/pan/sends use set_clip_mixer_envelope.

    Parameters:
    - track_index: The index of the track that owns the clip and device
    - clip_index: The index of the clip slot
    - device_index: The index of the device in the track's device chain
    - points: List of {"time": beats, "value": param value, "duration": beats (optional)}
    - parameter_index: Index of the parameter to automate (optional if parameter_name given)
    - parameter_name: Name of the parameter to automate (optional if parameter_index given)
    - clear_existing: Clear the parameter's existing envelope first (default True)
    - chain_index: If the device is a rack, index of the chain to descend into (optional)
    - chain_device_index: Index of the device within that chain (default 0, optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {
            "track_index": track_index, "clip_index": clip_index,
            "device_index": device_index, "points": points, "clear_existing": clear_existing,
        }
        if parameter_index is not None:
            params["parameter_index"] = parameter_index
        if parameter_name is not None:
            params["parameter_name"] = parameter_name
        if chain_index is not None:
            params["chain_index"] = chain_index
        if chain_device_index is not None:
            params["chain_device_index"] = chain_device_index
        result = ableton.send_command("set_clip_envelope", params)
        return (f"Wrote {result.get('point_count', len(points))} automation points for "
                f"'{result.get('parameter_name', '?')}' on '{result.get('device_name', '?')}' "
                f"in clip '{result.get('clip_name', clip_index)}'")
    except Exception as e:
        logger.error(f"Error setting clip envelope: {str(e)}")
        return f"Error setting clip envelope: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_clip_mixer_envelope")
def set_clip_mixer_envelope(
    ctx: Context,
    track_index: int,
    clip_index: int,
    target: str,
    points: List[Dict[str, float]],
    send_index: int = 0,
    clear_existing: bool = True,
    user_prompt: str = ""
) -> str:
    """
    Automate a track's mixer parameter (volume, pan, or a send) inside a clip.

    Parameters:
    - track_index: The index of the track that owns the clip
    - clip_index: The index of the clip slot
    - target: 'volume', 'pan', or 'send'
    - points: List of {"time": beats, "value": value, "duration": beats (optional)}.
      Value ranges: volume 0.0-1.0 (~0.85=0 dB), pan -1.0..1.0, send 0.0-1.0.
    - send_index: Which send when target is 'send' (0 = A, 1 = B, ...)
    - clear_existing: Clear the existing envelope first (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_mixer_envelope", {
            "track_index": track_index, "clip_index": clip_index, "target": target,
            "points": points, "send_index": send_index, "clear_existing": clear_existing,
        })
        return (f"Wrote {result.get('point_count', len(points))} automation points for "
                f"mixer {target} on clip '{result.get('clip_name', clip_index)}'")
    except Exception as e:
        logger.error(f"Error setting clip mixer envelope: {str(e)}")
        return f"Error setting clip mixer envelope: {str(e)}"


@mcp.tool()
@telemetry_tool("clear_clip_envelope")
def clear_clip_envelope(
    ctx: Context,
    track_index: int,
    clip_index: int,
    device_index: int,
    parameter_index: int = None,
    parameter_name: str = None,
    chain_index: int = None,
    chain_device_index: int = None,
    user_prompt: str = ""
) -> str:
    """
    Clear a device parameter's automation envelope inside a clip.

    Parameters:
    - track_index: The index of the track that owns the clip and device
    - clip_index: The index of the clip slot
    - device_index: The index of the device in the track's device chain
    - parameter_index: Index of the parameter (optional if parameter_name given)
    - parameter_name: Name of the parameter (optional if parameter_index given)
    - chain_index: If the device is a rack, index of the chain to descend into (optional)
    - chain_device_index: Index of the device within that chain (default 0, optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "clip_index": clip_index, "device_index": device_index}
        if parameter_index is not None:
            params["parameter_index"] = parameter_index
        if parameter_name is not None:
            params["parameter_name"] = parameter_name
        if chain_index is not None:
            params["chain_index"] = chain_index
        if chain_device_index is not None:
            params["chain_device_index"] = chain_device_index
        result = ableton.send_command("clear_clip_envelope", params)
        return f"Cleared envelope for '{result.get('parameter_name', '?')}' in clip '{result.get('clip_name', clip_index)}'"
    except Exception as e:
        logger.error(f"Error clearing clip envelope: {str(e)}")
        return f"Error clearing clip envelope: {str(e)}"


# ── Device management ──────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_device_chains")
def get_device_chains(ctx: Context, track_index: int, device_index: int, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Inspect a rack's chains and their nested devices (Instrument/Audio Effect/Drum Rack).

    Returns each chain's name and the devices inside it (index, name, class, whether it's
    itself a rack). Use the chain_index / chain_device_index from here with
    get_device_parameters and set_device_parameter to reach nested devices. Returns
    is_rack=false for non-rack devices.

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the (rack) device in the track's device chain
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_chains", {"track_index": track_index, "device_index": device_index, "track_type": track_type})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device chains: {str(e)}")
        return f"Error getting device chains: {str(e)}"


@mcp.tool()
@telemetry_tool("set_device_on")
def set_device_on(ctx: Context, track_index: int, device_index: int, on: bool = True, track_type: str = "regular", chain_index: int = None, chain_device_index: int = None, user_prompt: str = "") -> str:
    """
    Turn a device on or off (its 'Device On' switch).

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the device in the track's device chain
    - on: True to enable, False to bypass
    - track_type: 'regular' (default), 'return', or 'master'
    - chain_index: If the device is a rack, index of the chain to descend into (optional)
    - chain_device_index: Index of the device within that chain (default 0, optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index, "on": on, "track_type": track_type}
        if chain_index is not None:
            params["chain_index"] = chain_index
        if chain_device_index is not None:
            params["chain_device_index"] = chain_device_index
        result = ableton.send_command("set_device_on", params)
        return f"Set '{result.get('device_name', device_index)}' active={result.get('is_active')}"
    except Exception as e:
        logger.error(f"Error setting device on/off: {str(e)}")
        return f"Error setting device on/off: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_device")
def delete_device(ctx: Context, track_index: int, device_index: int, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Delete a device from a track's device chain.

    Parameters:
    - track_index: Index of the track holding the device
    - device_index: Index of the device to delete
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_device", {"track_index": track_index, "device_index": device_index, "track_type": track_type})
        return f"Deleted device '{result.get('deleted_name', device_index)}' ({result.get('device_count', '?')} devices remain)"
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"


# ── Transport / edit history ───────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("undo")
def undo(ctx: Context, user_prompt: str = "") -> str:
    """Undo the last action in Live.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("undo", {})
        return f"Undo done (can_undo={result.get('can_undo')}, can_redo={result.get('can_redo')})"
    except Exception as e:
        logger.error(f"Error during undo: {str(e)}")
        return f"Error during undo: {str(e)}"


@mcp.tool()
@telemetry_tool("redo")
def redo(ctx: Context, user_prompt: str = "") -> str:
    """Redo the last undone action in Live.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("redo", {})
        return f"Redo done (can_undo={result.get('can_undo')}, can_redo={result.get('can_redo')})"
    except Exception as e:
        logger.error(f"Error during redo: {str(e)}")
        return f"Error during redo: {str(e)}"


@mcp.tool()
@telemetry_tool("capture_midi")
def capture_midi(ctx: Context, user_prompt: str = "") -> str:
    """Capture recently played MIDI into a clip (Live's Capture MIDI feature).

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("capture_midi", {})
        return "Captured MIDI"
    except Exception as e:
        logger.error(f"Error capturing MIDI: {str(e)}")
        return f"Error capturing MIDI: {str(e)}"


# ── Colors, recording/transport, selection & view ──────────────────────────────

@mcp.tool()
@telemetry_tool("set_track_color")
def set_track_color(ctx: Context, track_index: int, color: int, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Set a track's color.

    Parameters:
    - track_index: Index of the track
    - color: RGB integer, e.g. 16711680 for red (0xFF0000), 65280 for green
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_color", {"track_index": track_index, "color": color, "track_type": track_type})
        return f"Set color of '{result.get('name', track_index)}' to {result.get('color')}"
    except Exception as e:
        logger.error(f"Error setting track color: {str(e)}")
        return f"Error setting track color: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_color")
def set_clip_color(ctx: Context, track_index: int, clip_index: int, color: int, user_prompt: str = "") -> str:
    """
    Set a clip's color.

    Parameters:
    - track_index: Index of the track containing the clip
    - clip_index: Index of the clip slot
    - color: RGB integer, e.g. 16711680 for red (0xFF0000)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_color", {"track_index": track_index, "clip_index": clip_index, "color": color})
        return f"Set color of clip '{result.get('clip_name', clip_index)}' to {result.get('color')}"
    except Exception as e:
        logger.error(f"Error setting clip color: {str(e)}")
        return f"Error setting clip color: {str(e)}"


@mcp.tool()
@telemetry_tool("set_scene_color")
def set_scene_color(ctx: Context, scene_index: int, color: int, user_prompt: str = "") -> str:
    """
    Set a scene's color.

    Parameters:
    - scene_index: Index of the scene
    - color: RGB integer, e.g. 16711680 for red (0xFF0000)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_scene_color", {"scene_index": scene_index, "color": color})
        return f"Set color of scene {scene_index} to {result.get('color')}"
    except Exception as e:
        logger.error(f"Error setting scene color: {str(e)}")
        return f"Error setting scene color: {str(e)}"


@mcp.tool()
@telemetry_tool("set_metronome")
def set_metronome(ctx: Context, on: bool = True, user_prompt: str = "") -> str:
    """Turn the metronome on or off.

    Parameters:
    - on: True to enable, False to disable
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_metronome", {"on": on})
        return f"Metronome {'on' if result.get('metronome') else 'off'}"
    except Exception as e:
        logger.error(f"Error setting metronome: {str(e)}")
        return f"Error setting metronome: {str(e)}"


@mcp.tool()
@telemetry_tool("set_arrangement_record")
def set_arrangement_record(ctx: Context, on: bool = True, user_prompt: str = "") -> str:
    """Arm/disarm Arrangement recording (the record button).

    Parameters:
    - on: True to enable arrangement record, False to disable
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_record", {"on": on})
        return f"Arrangement record mode = {result.get('record_mode')}"
    except Exception as e:
        logger.error(f"Error setting arrangement record: {str(e)}")
        return f"Error setting arrangement record: {str(e)}"


@mcp.tool()
@telemetry_tool("set_session_record")
def set_session_record(ctx: Context, on: bool = True, user_prompt: str = "") -> str:
    """Enable/disable Session recording.

    Parameters:
    - on: True to enable session record, False to disable
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_session_record", {"on": on})
        return f"Session record = {result.get('session_record')}"
    except Exception as e:
        logger.error(f"Error setting session record: {str(e)}")
        return f"Error setting session record: {str(e)}"


@mcp.tool()
@telemetry_tool("set_arrangement_loop")
def set_arrangement_loop(ctx: Context, enabled: bool = True, start: float = None, length: float = None, user_prompt: str = "") -> str:
    """
    Toggle the Arrangement loop and optionally set its start/length (in beats).

    Parameters:
    - enabled: True to enable the loop brace, False to disable
    - start: Loop start in beats (optional)
    - length: Loop length in beats (optional)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_loop", {"enabled": enabled, "start": start, "length": length})
        return f"Arrangement loop={result.get('loop')} start={result.get('loop_start')} length={result.get('loop_length')}"
    except Exception as e:
        logger.error(f"Error setting arrangement loop: {str(e)}")
        return f"Error setting arrangement loop: {str(e)}"


@mcp.tool()
@telemetry_tool("set_time_signature")
def set_time_signature(ctx: Context, numerator: int, denominator: int, user_prompt: str = "") -> str:
    """
    Set the song's time signature.

    Parameters:
    - numerator: Top number (e.g. 3 for 3/4)
    - denominator: Bottom number (e.g. 4 for 3/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_time_signature", {"numerator": numerator, "denominator": denominator})
        return f"Time signature = {result.get('numerator')}/{result.get('denominator')}"
    except Exception as e:
        logger.error(f"Error setting time signature: {str(e)}")
        return f"Error setting time signature: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_fold")
def set_track_fold(ctx: Context, track_index: int, folded: bool = True, user_prompt: str = "") -> str:
    """
    Fold or unfold a group track.

    Parameters:
    - track_index: Index of the group track
    - folded: True to collapse, False to expand
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_fold", {"track_index": track_index, "folded": folded})
        return f"Set fold of '{result.get('name', track_index)}' = {result.get('fold_state')}"
    except Exception as e:
        logger.error(f"Error setting track fold: {str(e)}")
        return f"Error setting track fold: {str(e)}"


@mcp.tool()
@telemetry_tool("select_track")
def select_track(ctx: Context, track_index: int, track_type: str = "regular", user_prompt: str = "") -> str:
    """
    Select a track in Live (makes it the visible/highlighted track).

    Parameters:
    - track_index: Index of the track
    - track_type: 'regular' (default), 'return', or 'master'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("select_track", {"track_index": track_index, "track_type": track_type})
        return f"Selected track '{result.get('selected_track', track_index)}'"
    except Exception as e:
        logger.error(f"Error selecting track: {str(e)}")
        return f"Error selecting track: {str(e)}"


@mcp.tool()
@telemetry_tool("select_scene")
def select_scene(ctx: Context, scene_index: int, user_prompt: str = "") -> str:
    """
    Select a scene in Live.

    Parameters:
    - scene_index: Index of the scene
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("select_scene", {"scene_index": scene_index})
        return f"Selected scene {scene_index}"
    except Exception as e:
        logger.error(f"Error selecting scene: {str(e)}")
        return f"Error selecting scene: {str(e)}"


@mcp.tool()
@telemetry_tool("show_view")
def show_view(ctx: Context, view: str, user_prompt: str = "") -> str:
    """
    Show a view in Live's UI.

    Parameters:
    - view: One of 'Browser', 'Arranger', 'Session', 'Detail', 'Detail/Clip', 'Detail/DeviceChain'
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("show_view", {"view": view})
        return f"Showing view '{result.get('view', view)}'"
    except Exception as e:
        logger.error(f"Error showing view: {str(e)}")
        return f"Error showing view: {str(e)}"


# ── Batch ──────────────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("batch")
def batch(ctx: Context, operations: List[Dict[str, Any]], stop_on_error: bool = False, user_prompt: str = "") -> str:
    """
    Run many Ableton operations in a single round-trip. Much faster than calling
    tools one at a time when building or editing a lot at once (tracks, clips,
    notes, mixer, automation, etc.).

    Each operation is a dict: {"type": <command>, "params": {...}}, where <command>
    is the underlying Ableton command name and params match that command. Common
    command names mirror the individual tools, e.g.:
      - "create_midi_track" {"index": -1}
      - "create_audio_track" {"index": -1}
      - "set_track_name" {"track_index": 0, "name": "Bass"}
      - "create_clip" {"track_index": 0, "clip_index": 0, "length": 4.0}
      - "add_notes_to_clip" {"track_index": 0, "clip_index": 0, "notes": [...]}
      - "set_track_volume" {"track_index": 0, "value": 0.8}
      - "set_device_parameter" {"track_index": 0, "device_index": 0, "parameter_name": "Frequency", "value": 0.5}
      - "set_clip_envelope" {"track_index": 0, "clip_index": 0, "device_index": 0, "parameter_name": "Frequency", "points": [...]}
    Read commands (e.g. "get_track_info", "get_device_parameters") are allowed too.

    Operations run in order on Live's main thread. Each result is returned
    individually, so one failure doesn't discard the others' outcomes.

    Parameters:
    - operations: List of {"type": command, "params": {...}} dicts
    - stop_on_error: If True, stop at the first failing operation (default False)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("batch", {"operations": operations, "stop_on_error": stop_on_error})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error running batch: {str(e)}")
        return f"Error running batch: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()