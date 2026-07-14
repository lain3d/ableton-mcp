# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import Live
import os
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication.
# Bind to loopback only so the command server isn't reachable from the network.
DEFAULT_PORT = 9877
HOST = "127.0.0.1"

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message("AbletonMCP Remote Script initializing...")
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()

        # State-observer buffer: Live listeners push change events here and the
        # client drains them with poll_events (keeps request/response framing).
        self._event_queue = []
        self._event_lock = threading.Lock()
        self._event_seq = 0
        self._subscriptions = {}  # target -> [(obj, remove_method_name, callback)]

        # Start the socket server
        self.start_server()
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False

        # Remove any Live listeners we registered.
        try:
            self._unsubscribe_all()
        except Exception:
            pass

        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        buffer = ''  # Changed from b'' to '' for Python 2
        
        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)
                    
                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break
                    
                    # Accumulate data in buffer with explicit encoding/decoding
                    try:
                        # Python 3: data is bytes, decode to string
                        buffer += data.decode('utf-8')
                    except AttributeError:
                        # Python 2: data is already string
                        buffer += data
                    
                    try:
                        # Try to parse command from buffer
                        command = json.loads(buffer)  # Removed decode('utf-8')
                        buffer = ''  # Clear buffer after successful parse
                        
                        self.log_message("Received command: " + str(command.get("type", "unknown")))
                        
                        # Process the command and get response
                        response = self._process_command(command)
                        
                        # Send the response with explicit encoding
                        try:
                            # Python 3: encode string to bytes
                            client.sendall(json.dumps(response).encode('utf-8'))
                        except AttributeError:
                            # Python 2: string is already bytes
                            client.sendall(json.dumps(response))
                    except ValueError:
                        # Incomplete data, wait for more
                        continue
                        
                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())
                    
                    # Send error response if possible
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        # Python 3: encode string to bytes
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except AttributeError:
                        # Python 2: string is already bytes
                        client.sendall(json.dumps(error_response))
                    except:
                        # If we can't send the error, the connection is probably dead
                        break
                    
                    # For serious errors, break the loop
                    if not isinstance(e, ValueError):
                        break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    # Commands that mutate Live state and must run on the main thread.
    MODIFYING_COMMANDS = frozenset([
        "create_midi_track", "set_track_name",
        "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
        "set_tempo", "fire_clip", "stop_clip",
        "start_playback", "stop_playback", "load_browser_item",
        "create_audio_track", "create_return_track",
        "set_track_volume", "set_track_pan", "set_track_send",
        "set_track_mute", "set_track_solo", "set_track_arm",
        "delete_track", "duplicate_track", "set_device_parameter",
        "delete_device", "set_device_on",
        "remove_clip_notes", "quantize_clip", "set_clip_loop",
        "set_clip_gain", "set_clip_pitch", "set_clip_warp",
        "create_scene", "delete_scene", "duplicate_scene", "fire_scene", "set_scene_name",
        "delete_clip", "duplicate_clip",
        "set_track_input_routing", "set_track_output_routing",
        "set_clip_envelope", "set_clip_mixer_envelope", "clear_clip_envelope",
        "undo", "redo", "capture_midi",
        "set_track_color", "set_clip_color", "set_scene_color",
        "set_metronome", "set_arrangement_record", "set_session_record",
        "set_arrangement_loop", "set_time_signature", "set_track_fold",
        "select_track", "select_scene", "show_view",
        "subscribe", "unsubscribe",
        "switch_to_arrangement_view", "set_current_song_time",
        "duplicate_session_clip_to_arrangement",
    ])

    # Per-command socket/queue budget (seconds) for operations slower than the default.
    LONG_RUNNING_COMMANDS = {"create_audio_clip": 60.0}

    def _execute(self, command_type, params):
        """Run a single command and return its result (raising on error).

        Shared by the direct dispatch and by batch. Callers are responsible for
        scheduling MODIFYING_COMMANDS on the main thread.
        """
        if command_type == "batch":
            return self._batch(params.get("operations", []), params.get("stop_on_error", False))
        # Reads
        elif command_type == "get_session_info":
            return self._get_session_info()
        elif command_type == "get_track_info":
            return self._get_track_info(params.get("track_index", 0))
        elif command_type == "get_browser_item":
            return self._get_browser_item(params.get("uri", None), params.get("path", None))
        elif command_type == "get_browser_categories":
            return self._get_browser_categories(params.get("category_type", "all"))
        elif command_type == "get_browser_items":
            return self._get_browser_items(params.get("path", ""), params.get("item_type", "all"))
        elif command_type == "get_browser_tree":
            return self.get_browser_tree(params.get("category_type", "all"))
        elif command_type == "get_browser_items_at_path":
            return self.get_browser_items_at_path(params.get("path", ""))
        elif command_type == "get_arrangement_clips":
            return self._get_arrangement_clips(params.get("track_index", 0))
        elif command_type == "get_device_parameters":
            return self._get_device_parameters(params.get("track_index", 0),
                                               params.get("device_index", 0),
                                               params.get("track_type", "regular"),
                                               params.get("chain_index", None),
                                               params.get("chain_device_index", None))
        elif command_type == "get_device_chains":
            return self._get_device_chains(params.get("track_index", 0),
                                           params.get("device_index", 0),
                                           params.get("track_type", "regular"))
        elif command_type == "get_clip_notes":
            return self._get_clip_notes(params.get("track_index", 0), params.get("clip_index", 0))
        elif command_type == "get_track_routing":
            return self._get_track_routing(params.get("track_index", 0))
        # Tracks / clips / notes
        elif command_type == "create_midi_track":
            return self._create_midi_track(params.get("index", -1))
        elif command_type == "set_track_name":
            return self._set_track_name(params.get("track_index", 0), params.get("name", ""))
        elif command_type == "create_clip":
            return self._create_clip(params.get("track_index", 0), params.get("clip_index", 0),
                                     params.get("length", 4.0))
        elif command_type == "create_audio_clip":
            return self._create_audio_clip(params.get("track_index", 0), params.get("clip_index", 0),
                                           params.get("path", ""))
        elif command_type == "add_notes_to_clip":
            return self._add_notes_to_clip(params.get("track_index", 0), params.get("clip_index", 0),
                                           params.get("notes", []))
        elif command_type == "set_clip_name":
            return self._set_clip_name(params.get("track_index", 0), params.get("clip_index", 0),
                                       params.get("name", ""))
        elif command_type == "set_tempo":
            return self._set_tempo(params.get("tempo", 120.0))
        elif command_type == "fire_clip":
            return self._fire_clip(params.get("track_index", 0), params.get("clip_index", 0))
        elif command_type == "stop_clip":
            return self._stop_clip(params.get("track_index", 0), params.get("clip_index", 0))
        elif command_type == "start_playback":
            return self._start_playback()
        elif command_type == "stop_playback":
            return self._stop_playback()
        elif command_type == "load_browser_item":
            return self._load_browser_item(params.get("track_index", 0), params.get("item_uri", ""))
        # Audio tracks, mixer & device parameters
        elif command_type == "create_audio_track":
            return self._create_audio_track(params.get("index", -1))
        elif command_type == "create_return_track":
            return self._create_return_track()
        elif command_type == "set_track_volume":
            return self._set_track_volume(params.get("track_index", 0), params.get("value", 0.85),
                                          params.get("track_type", "regular"))
        elif command_type == "set_track_pan":
            return self._set_track_pan(params.get("track_index", 0), params.get("value", 0.0),
                                       params.get("track_type", "regular"))
        elif command_type == "set_track_send":
            return self._set_track_send(params.get("track_index", 0), params.get("send_index", 0),
                                        params.get("value", 0.0))
        elif command_type == "set_track_mute":
            return self._set_track_mute(params.get("track_index", 0), params.get("mute", True),
                                        params.get("track_type", "regular"))
        elif command_type == "set_track_solo":
            return self._set_track_solo(params.get("track_index", 0), params.get("solo", True),
                                        params.get("track_type", "regular"))
        elif command_type == "set_track_arm":
            return self._set_track_arm(params.get("track_index", 0), params.get("arm", True))
        elif command_type == "delete_track":
            return self._delete_track(params.get("track_index", 0))
        elif command_type == "duplicate_track":
            return self._duplicate_track(params.get("track_index", 0))
        elif command_type == "set_device_parameter":
            return self._set_device_parameter(params.get("track_index", 0), params.get("device_index", 0),
                                              params.get("value", 0.0), params.get("parameter_index", None),
                                              params.get("parameter_name", None),
                                              params.get("track_type", "regular"),
                                              params.get("chain_index", None),
                                              params.get("chain_device_index", None))
        elif command_type == "delete_device":
            return self._delete_device(params.get("track_index", 0), params.get("device_index", 0),
                                       params.get("track_type", "regular"))
        elif command_type == "set_device_on":
            return self._set_device_on(params.get("track_index", 0), params.get("device_index", 0),
                                       params.get("on", True), params.get("track_type", "regular"),
                                       params.get("chain_index", None),
                                       params.get("chain_device_index", None))
        # Clip content
        elif command_type == "remove_clip_notes":
            return self._remove_clip_notes(params.get("track_index", 0), params.get("clip_index", 0),
                                           params.get("from_time", 0.0), params.get("from_pitch", 0),
                                           params.get("time_span", 1000000.0), params.get("pitch_span", 128))
        elif command_type == "quantize_clip":
            return self._quantize_clip(params.get("track_index", 0), params.get("clip_index", 0),
                                       params.get("grid", "1/16"), params.get("amount", 1.0))
        elif command_type == "set_clip_loop":
            return self._set_clip_loop(params.get("track_index", 0), params.get("clip_index", 0),
                                       params.get("looping", True), params.get("loop_start", None),
                                       params.get("loop_end", None))
        elif command_type == "set_clip_gain":
            return self._set_clip_gain(params.get("track_index", 0), params.get("clip_index", 0),
                                       params.get("gain", 0.5))
        elif command_type == "set_clip_pitch":
            return self._set_clip_pitch(params.get("track_index", 0), params.get("clip_index", 0),
                                        params.get("coarse", 0), params.get("fine", 0))
        elif command_type == "set_clip_warp":
            return self._set_clip_warp(params.get("track_index", 0), params.get("clip_index", 0),
                                       params.get("warping", True), params.get("warp_mode", None))
        # Scenes, clip management, routing, automation, transport
        elif command_type == "create_scene":
            return self._create_scene(params.get("index", -1))
        elif command_type == "delete_scene":
            return self._delete_scene(params.get("scene_index", 0))
        elif command_type == "duplicate_scene":
            return self._duplicate_scene(params.get("scene_index", 0))
        elif command_type == "fire_scene":
            return self._fire_scene(params.get("scene_index", 0))
        elif command_type == "set_scene_name":
            return self._set_scene_name(params.get("scene_index", 0), params.get("name", ""))
        elif command_type == "delete_clip":
            return self._delete_clip(params.get("track_index", 0), params.get("clip_index", 0))
        elif command_type == "duplicate_clip":
            return self._duplicate_clip(params.get("track_index", 0), params.get("clip_index", 0),
                                        params.get("target_clip_index", None))
        elif command_type == "set_track_input_routing":
            return self._set_track_input_routing(params.get("track_index", 0),
                                                 params.get("routing_name", None), params.get("channel", None))
        elif command_type == "set_track_output_routing":
            return self._set_track_output_routing(params.get("track_index", 0),
                                                  params.get("routing_name", None), params.get("channel", None))
        elif command_type == "set_clip_envelope":
            return self._set_clip_envelope(params.get("track_index", 0), params.get("clip_index", 0),
                                           params.get("device_index", 0), params.get("points", []),
                                           params.get("parameter_index", None), params.get("parameter_name", None),
                                           params.get("clear_existing", True),
                                           params.get("chain_index", None),
                                           params.get("chain_device_index", None))
        elif command_type == "set_clip_mixer_envelope":
            return self._set_clip_mixer_envelope(params.get("track_index", 0), params.get("clip_index", 0),
                                                 params.get("target", "volume"), params.get("points", []),
                                                 params.get("send_index", 0),
                                                 params.get("clear_existing", True))
        elif command_type == "clear_clip_envelope":
            return self._clear_clip_envelope(params.get("track_index", 0), params.get("clip_index", 0),
                                             params.get("device_index", 0), params.get("parameter_index", None),
                                             params.get("parameter_name", None),
                                             params.get("chain_index", None),
                                             params.get("chain_device_index", None))
        elif command_type == "undo":
            return self._undo()
        elif command_type == "redo":
            return self._redo()
        elif command_type == "capture_midi":
            return self._capture_midi()
        # Colors, recording/transport, selection & view
        elif command_type == "set_track_color":
            return self._set_track_color(params.get("track_index", 0), params.get("color", 0),
                                         params.get("track_type", "regular"))
        elif command_type == "set_clip_color":
            return self._set_clip_color(params.get("track_index", 0), params.get("clip_index", 0),
                                        params.get("color", 0))
        elif command_type == "set_scene_color":
            return self._set_scene_color(params.get("scene_index", 0), params.get("color", 0))
        elif command_type == "set_metronome":
            return self._set_metronome(params.get("on", True))
        elif command_type == "set_arrangement_record":
            return self._set_arrangement_record(params.get("on", True))
        elif command_type == "set_session_record":
            return self._set_session_record(params.get("on", True))
        elif command_type == "set_arrangement_loop":
            return self._set_arrangement_loop(params.get("enabled", True),
                                              params.get("start", None), params.get("length", None))
        elif command_type == "set_time_signature":
            return self._set_time_signature(params.get("numerator", 4), params.get("denominator", 4))
        elif command_type == "set_track_fold":
            return self._set_track_fold(params.get("track_index", 0), params.get("folded", True))
        elif command_type == "select_track":
            return self._select_track(params.get("track_index", 0), params.get("track_type", "regular"))
        elif command_type == "select_scene":
            return self._select_scene(params.get("scene_index", 0))
        elif command_type == "show_view":
            return self._show_view(params.get("view", "Session"))
        # State observers
        elif command_type == "subscribe":
            return self._subscribe(params.get("targets", []))
        elif command_type == "unsubscribe":
            return self._unsubscribe(params.get("targets", []))
        elif command_type == "poll_events":
            return self._poll_events(params.get("max_events", 100), params.get("clear", True))
        elif command_type == "list_subscriptions":
            return self._list_subscriptions()
        # Arrangement view
        elif command_type == "switch_to_arrangement_view":
            return self._switch_to_arrangement_view()
        elif command_type == "set_current_song_time":
            return self._set_current_song_time(params.get("time", 0.0))
        elif command_type == "duplicate_session_clip_to_arrangement":
            return self._duplicate_session_clip_to_arrangement(params.get("track_index", 0),
                                                               params.get("clip_index", 0),
                                                               params.get("destination_time", 0.0))
        else:
            raise ValueError("Unknown command: " + str(command_type))

    def _batch(self, operations, stop_on_error=False):
        """Run a list of operations in order in a single main-thread pass.

        Each operation is {"type": command_type, "params": {...}}. Results are
        collected per-op so one failure doesn't lose the others' outcomes. The
        whole batch is grouped into one undo step so a single undo reverts it.
        """
        song = self._song
        # Skip grouping if the batch itself calls undo/redo, which would operate
        # on a half-open undo step.
        types = set(op.get("type", "") for op in operations)
        group = (hasattr(song, "begin_undo_step") and hasattr(song, "end_undo_step")
                 and not (types & set(("undo", "redo"))))
        if group:
            try:
                song.begin_undo_step()
            except Exception:
                group = False

        results = []
        try:
            for op in operations:
                op_type = op.get("type", "")
                op_params = op.get("params", {})
                try:
                    res = self._execute(op_type, op_params)
                    results.append({"status": "success", "type": op_type, "result": res})
                except Exception as e:
                    self.log_message("Batch op '%s' failed: %s" % (op_type, str(e)))
                    results.append({"status": "error", "type": op_type, "message": str(e)})
                    if stop_on_error:
                        break
        finally:
            if group:
                try:
                    song.end_undo_step()
                except Exception:
                    pass

        succeeded = sum(1 for r in results if r["status"] == "success")
        return {"operation_count": len(operations), "succeeded": succeeded,
                "grouped_undo": group, "results": results}

    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Batch and any state-mutating command run on Live's main thread;
            # every read is handled directly by the shared dispatch below.
            if command_type == "batch" or command_type in self.MODIFYING_COMMANDS:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = self._execute(command_type, params)
                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # Wait for the response with a timeout. Some commands (notably
                # create_audio_clip, which decodes/imports the audio file on
                # the main thread) can take longer than the default 10s on
                # larger files; a batch scales with the work it queues.
                if command_type == "batch":
                    ops = params.get("operations", [])
                    queue_timeout = max(30.0, 2.0 * len(ops) + sum(
                        self.LONG_RUNNING_COMMANDS.get(o.get("type", ""), 0.0) for o in ops))
                else:
                    queue_timeout = self.LONG_RUNNING_COMMANDS.get(command_type, 10.0)
                try:
                    task_response = response_queue.get(timeout=queue_timeout)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            else:
                # All read-only commands run directly via the shared dispatch
                # (raises "Unknown command" for genuinely unknown ones).
                response["result"] = self._execute(command_type, params)
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Command implementations
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Transport / playback state — lets clients render a live
                # playhead without polling separately. Each property is read
                # via _safe_song_property so an attribute missing on a given
                # Live version falls back to its default rather than breaking
                # the response shape.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    clip_info = {
                        "name": clip.name,
                        "length": clip.length,
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": track.arm,
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise
    
    
    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            # Must be an audio track. Audio tracks expose audio input; MIDI
            # tracks don't. Reject MIDI / return tracks up front so the caller
            # gets a clear error instead of a Live API exception.
            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip

            # Prefer the Live 11+ note API, which carries per-note expression
            # (probability, velocity deviation, release velocity). Fall back to
            # the legacy tuple API on older Live versions.
            spec_cls = getattr(Live.Clip, "MidiNoteSpecification", None)
            if hasattr(clip, "add_new_notes") and spec_cls is not None:
                specs = []
                for note in notes:
                    kwargs = dict(
                        pitch=int(note.get("pitch", 60)),
                        start_time=float(note.get("start_time", 0.0)),
                        duration=float(note.get("duration", 0.25)),
                        velocity=float(note.get("velocity", 100)),
                        mute=bool(note.get("mute", False)),
                        probability=float(note.get("probability", 1.0)),
                        velocity_deviation=float(note.get("velocity_deviation", 0.0)),
                        release_velocity=float(note.get("release_velocity", 64)),
                    )
                    try:
                        specs.append(spec_cls(**kwargs))
                    except TypeError:
                        # Older MidiNoteSpecification without the expression fields.
                        specs.append(spec_cls(
                            pitch=kwargs["pitch"], start_time=kwargs["start_time"],
                            duration=kwargs["duration"], velocity=kwargs["velocity"],
                            mute=kwargs["mute"]))
                clip.add_new_notes(tuple(specs))
            else:
                live_notes = []
                for note in notes:
                    live_notes.append((int(note.get("pitch", 60)),
                                       float(note.get("start_time", 0.0)),
                                       float(note.get("duration", 0.25)),
                                       int(note.get("velocity", 100)),
                                       bool(note.get("mute", False))))
                clip.set_notes(tuple(live_notes))

            return {"note_count": len(notes)}
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise
    
    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise
    
    
    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playing the session"""
        try:
            self._song.stop_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error stopping playback: " + str(e))
            raise
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = float(time_val)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    # ── Mixer / track management / device parameters ──────────────────────────

    def _get_track_by_index(self, track_index, track_type="regular"):
        """Resolve a track from a (type, index) pair.

        track_type: "regular" (song.tracks), "return" (song.return_tracks),
        or "master" (song.master_track — track_index ignored).
        """
        if track_type == "master":
            return self._song.master_track
        if track_type == "return":
            tracks = self._song.return_tracks
        else:
            tracks = self._song.tracks
        if track_index < 0 or track_index >= len(tracks):
            raise IndexError("Track index out of range")
        return tracks[track_index]

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index (-1 = end)."""
        try:
            self._song.create_audio_track(index)
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            return {"index": new_track_index, "name": new_track.name}
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise

    def _create_return_track(self):
        """Create a new return track (appended after existing returns)."""
        try:
            self._song.create_return_track()
            new_index = len(self._song.return_tracks) - 1
            new_track = self._song.return_tracks[new_index]
            return {"index": new_index, "name": new_track.name}
        except Exception as e:
            self.log_message("Error creating return track: " + str(e))
            raise

    def _set_track_volume(self, track_index, value, track_type="regular"):
        """Set track volume. value is normalized 0.0-1.0 (0.85 ~= 0 dB)."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            param = track.mixer_device.volume
            value = max(param.min, min(param.max, float(value)))
            param.value = value
            return {"name": track.name, "volume": param.value}
        except Exception as e:
            self.log_message("Error setting track volume: " + str(e))
            raise

    def _set_track_pan(self, track_index, value, track_type="regular"):
        """Set track pan. value -1.0 (hard L) .. 0.0 (center) .. 1.0 (hard R)."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            param = track.mixer_device.panning
            value = max(param.min, min(param.max, float(value)))
            param.value = value
            return {"name": track.name, "panning": param.value}
        except Exception as e:
            self.log_message("Error setting track pan: " + str(e))
            raise

    def _set_track_send(self, track_index, send_index, value):
        """Set a send level on a regular track. value normalized 0.0-1.0."""
        try:
            track = self._get_track_by_index(track_index, "regular")
            sends = track.mixer_device.sends
            if send_index < 0 or send_index >= len(sends):
                raise IndexError("Send index out of range")
            param = sends[send_index]
            value = max(param.min, min(param.max, float(value)))
            param.value = value
            return {"name": track.name, "send_index": send_index, "value": param.value}
        except Exception as e:
            self.log_message("Error setting track send: " + str(e))
            raise

    def _set_track_mute(self, track_index, mute, track_type="regular"):
        try:
            track = self._get_track_by_index(track_index, track_type)
            track.mute = bool(mute)
            return {"name": track.name, "mute": track.mute}
        except Exception as e:
            self.log_message("Error setting track mute: " + str(e))
            raise

    def _set_track_solo(self, track_index, solo, track_type="regular"):
        try:
            track = self._get_track_by_index(track_index, track_type)
            track.solo = bool(solo)
            return {"name": track.name, "solo": track.solo}
        except Exception as e:
            self.log_message("Error setting track solo: " + str(e))
            raise

    def _set_track_arm(self, track_index, arm):
        try:
            track = self._get_track_by_index(track_index, "regular")
            if not getattr(track, "can_be_armed", False):
                raise ValueError("Track %d cannot be armed" % track_index)
            track.arm = bool(arm)
            return {"name": track.name, "arm": track.arm}
        except Exception as e:
            self.log_message("Error setting track arm: " + str(e))
            raise

    def _delete_track(self, track_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            name = self._song.tracks[track_index].name
            self._song.delete_track(track_index)
            return {"deleted_index": track_index, "deleted_name": name,
                    "track_count": len(self._song.tracks)}
        except Exception as e:
            self.log_message("Error deleting track: " + str(e))
            raise

    def _duplicate_track(self, track_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            self._song.duplicate_track(track_index)
            # Live inserts the duplicate immediately after the source track.
            new_index = track_index + 1
            new_track = self._song.tracks[new_index]
            return {"index": new_index, "name": new_track.name}
        except Exception as e:
            self.log_message("Error duplicating track: " + str(e))
            raise

    def _resolve_device(self, track, device_index, chain_index=None, chain_device_index=None):
        """Resolve a device on a track, optionally descending into a rack chain."""
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        device = track.devices[device_index]
        if chain_index is not None:
            chains = getattr(device, "chains", None)
            if not chains:
                raise ValueError("Device '%s' has no chains" % device.name)
            if chain_index < 0 or chain_index >= len(chains):
                raise IndexError("Chain index out of range")
            chain = chains[chain_index]
            cd = 0 if chain_device_index is None else chain_device_index
            if cd < 0 or cd >= len(chain.devices):
                raise IndexError("Chain device index out of range")
            device = chain.devices[cd]
        return device

    def _resolve_param_on_device(self, device, parameter_index, parameter_name):
        if parameter_name is not None:
            lname = str(parameter_name).lower()
            for p in device.parameters:
                if p.name.lower() == lname:
                    return p
            raise ValueError("Parameter '%s' not found on device '%s'"
                             % (parameter_name, device.name))
        if parameter_index is None:
            raise ValueError("Provide parameter_index or parameter_name")
        if parameter_index < 0 or parameter_index >= len(device.parameters):
            raise IndexError("Parameter index out of range")
        return device.parameters[parameter_index]

    def _get_device_parameters(self, track_index, device_index, track_type="regular",
                               chain_index=None, chain_device_index=None):
        """List a device's parameters with current value, range and display value."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            device = self._resolve_device(track, device_index, chain_index, chain_device_index)
            params = []
            for i, p in enumerate(device.parameters):
                info = {
                    "index": i,
                    "name": p.name,
                    "value": p.value,
                    "min": p.min,
                    "max": p.max,
                    "is_quantized": p.is_quantized,
                }
                try:
                    info["display_value"] = str(p.str_for_value(p.value))
                except Exception:
                    pass
                if p.is_quantized:
                    try:
                        info["value_items"] = [str(v) for v in p.value_items]
                    except Exception:
                        pass
                params.append(info)
            return {
                "track_name": track.name,
                "device_index": device_index,
                "device_name": device.name,
                "class_name": device.class_name,
                "parameters": params,
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, value,
                              parameter_index=None, parameter_name=None,
                              track_type="regular", chain_index=None,
                              chain_device_index=None):
        """Set a device parameter by index or by (case-insensitive) name."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            device = self._resolve_device(track, device_index, chain_index, chain_device_index)
            param = self._resolve_param_on_device(device, parameter_index, parameter_name)

            if not getattr(param, "is_enabled", True):
                raise ValueError("Parameter '%s' is not automatable" % param.name)

            value = max(param.min, min(param.max, float(value)))
            param.value = value
            result = {
                "device_name": device.name,
                "parameter_name": param.name,
                "value": param.value,
            }
            try:
                result["display_value"] = str(param.str_for_value(param.value))
            except Exception:
                pass
            return result
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def _delete_device(self, track_index, device_index, track_type="regular"):
        try:
            track = self._get_track_by_index(track_index, track_type)
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            name = track.devices[device_index].name
            track.delete_device(device_index)
            return {"deleted_name": name, "device_count": len(track.devices)}
        except Exception as e:
            self.log_message("Error deleting device: " + str(e))
            raise

    def _set_device_on(self, track_index, device_index, on, track_type="regular",
                       chain_index=None, chain_device_index=None):
        """Toggle a device on/off via its 'Device On' parameter."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            device = self._resolve_device(track, device_index, chain_index, chain_device_index)
            param = None
            for p in device.parameters:
                if p.name == "Device On":
                    param = p
                    break
            if param is None and len(device.parameters) > 0:
                param = device.parameters[0]
            if param is None:
                raise ValueError("Device '%s' has no on/off parameter" % device.name)
            param.value = 1.0 if on else 0.0
            return {"device_name": device.name, "is_active": bool(device.is_active)}
        except Exception as e:
            self.log_message("Error setting device on/off: " + str(e))
            raise

    def _get_device_chains(self, track_index, device_index, track_type="regular"):
        """Read a rack's chains and their nested devices (empty for non-racks)."""
        try:
            track = self._get_track_by_index(track_index, track_type)
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]
            chains = getattr(device, "chains", None)
            if not chains:
                return {"device_name": device.name, "is_rack": False, "chains": []}
            out = []
            for ci, chain in enumerate(chains):
                devs = [{"index": di, "name": d.name, "class_name": d.class_name,
                         "num_parameters": len(d.parameters),
                         "is_rack": bool(getattr(d, "chains", None))}
                        for di, d in enumerate(chain.devices)]
                out.append({"index": ci, "name": chain.name, "devices": devs})
            return {"device_name": device.name, "is_rack": True,
                    "chain_count": len(out), "chains": out}
        except Exception as e:
            self.log_message("Error getting device chains: " + str(e))
            raise

    # ── Clip content: notes, quantize, loop & audio warp ──────────────────────

    _QUANTIZATION_GRID = {
        "1/4": "q_quarter",
        "1/4t": "q_quarter_triplet",
        "1/8": "q_eighth",
        "1/8t": "q_eighth_triplet",
        "1/16": "q_sixteenth",
        "1/16t": "q_sixteenth_triplet",
        "1/32": "q_thirtysecond",
    }

    def _get_clip(self, track_index, clip_index):
        """Resolve a Session-view clip, raising a clear error if empty."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if clip_index < 0 or clip_index >= len(track.clip_slots):
            raise IndexError("Clip index out of range")
        clip_slot = track.clip_slots[clip_index]
        if not clip_slot.has_clip:
            raise Exception("No clip in slot")
        return clip_slot.clip

    def _read_notes(self, clip):
        """Return a clip's notes as dicts, preferring the Live 11+ extended API.

        The legacy get_notes/remove_notes calls are deprecated and the Remote
        Script runtime raises their DeprecationWarning as an error, so use the
        extended API when available and fall back only on older Live versions.
        """
        # Use a large time span rather than clip.length: a looping clip reports
        # its loop length, which would hide notes placed beyond the loop end.
        span = 1000000.0
        if hasattr(clip, "get_notes_extended"):
            return [
                {"pitch": n.pitch, "start_time": n.start_time, "duration": n.duration,
                 "velocity": n.velocity, "mute": n.mute,
                 "probability": getattr(n, "probability", 1.0),
                 "velocity_deviation": getattr(n, "velocity_deviation", 0.0),
                 "release_velocity": getattr(n, "release_velocity", 64)}
                for n in clip.get_notes_extended(0, 128, 0.0, span)
            ]
        return [
            {"pitch": t[0], "start_time": t[1], "duration": t[2],
             "velocity": t[3], "mute": t[4]}
            for t in clip.get_notes(0.0, 0, span, 128)
        ]

    def _get_clip_notes(self, track_index, clip_index):
        """Read all MIDI notes from a clip as pitch/start/duration/velocity/mute."""
        try:
            clip = self._get_clip(track_index, clip_index)
            if not clip.is_midi_clip:
                raise ValueError("Clip is not a MIDI clip")
            notes = self._read_notes(clip)
            return {"clip_name": clip.name, "note_count": len(notes), "notes": notes}
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _remove_clip_notes(self, track_index, clip_index, from_time, from_pitch,
                           time_span, pitch_span):
        """Remove MIDI notes within a time/pitch window (defaults clear everything)."""
        try:
            clip = self._get_clip(track_index, clip_index)
            if not clip.is_midi_clip:
                raise ValueError("Clip is not a MIDI clip")
            # Note the extended API's argument order: (pitch, pitch_span, time, time_span).
            if hasattr(clip, "remove_notes_extended"):
                clip.remove_notes_extended(int(from_pitch), int(pitch_span),
                                           from_time, time_span)
            else:
                clip.remove_notes(from_time, int(from_pitch), time_span, int(pitch_span))
            remaining = len(self._read_notes(clip))
            return {"clip_name": clip.name, "remaining_note_count": remaining}
        except Exception as e:
            self.log_message("Error removing clip notes: " + str(e))
            raise

    def _quantize_clip(self, track_index, clip_index, grid, amount):
        """Quantize a clip to a grid. amount is 0.0-1.0 (1.0 = full quantize)."""
        try:
            clip = self._get_clip(track_index, clip_index)
            enum_name = self._QUANTIZATION_GRID.get(grid)
            if enum_name is None:
                raise ValueError("Unknown grid '%s' (use one of %s)"
                                 % (grid, ", ".join(sorted(self._QUANTIZATION_GRID))))
            quant = getattr(Live.Song.Quantization, enum_name)
            amount = max(0.0, min(1.0, float(amount)))
            clip.quantize(quant, amount)
            return {"clip_name": clip.name, "grid": grid, "amount": amount}
        except Exception as e:
            self.log_message("Error quantizing clip: " + str(e))
            raise

    def _set_clip_loop(self, track_index, clip_index, looping,
                       loop_start=None, loop_end=None):
        """Toggle looping and optionally set loop start/end (in beats)."""
        try:
            clip = self._get_clip(track_index, clip_index)
            clip.looping = bool(looping)
            # end must move before start when shrinking, so order the writes.
            if loop_end is not None and loop_start is not None:
                if loop_start <= clip.loop_end:
                    clip.loop_start = float(loop_start)
                    clip.loop_end = float(loop_end)
                else:
                    clip.loop_end = float(loop_end)
                    clip.loop_start = float(loop_start)
            elif loop_start is not None:
                clip.loop_start = float(loop_start)
            elif loop_end is not None:
                clip.loop_end = float(loop_end)
            return {"clip_name": clip.name, "looping": clip.looping,
                    "loop_start": clip.loop_start, "loop_end": clip.loop_end}
        except Exception as e:
            self.log_message("Error setting clip loop: " + str(e))
            raise

    def _set_clip_gain(self, track_index, clip_index, gain):
        """Set an audio clip's gain (normalized 0.0-1.0)."""
        try:
            clip = self._get_clip(track_index, clip_index)
            if not clip.is_audio_clip:
                raise ValueError("Clip is not an audio clip")
            clip.gain = max(0.0, min(1.0, float(gain)))
            result = {"clip_name": clip.name, "gain": clip.gain}
            try:
                result["gain_display"] = str(clip.gain_display_string)
            except Exception:
                pass
            return result
        except Exception as e:
            self.log_message("Error setting clip gain: " + str(e))
            raise

    def _set_clip_pitch(self, track_index, clip_index, coarse, fine):
        """Transpose an audio clip: coarse in semitones, fine in cents."""
        try:
            clip = self._get_clip(track_index, clip_index)
            if not clip.is_audio_clip:
                raise ValueError("Clip is not an audio clip")
            clip.pitch_coarse = max(-48, min(48, int(coarse)))
            clip.pitch_fine = max(-50, min(50, int(fine)))
            return {"clip_name": clip.name, "pitch_coarse": clip.pitch_coarse,
                    "pitch_fine": clip.pitch_fine}
        except Exception as e:
            self.log_message("Error setting clip pitch: " + str(e))
            raise

    def _set_clip_warp(self, track_index, clip_index, warping, warp_mode=None):
        """Toggle warping on an audio clip and optionally set the warp mode index."""
        try:
            clip = self._get_clip(track_index, clip_index)
            if not clip.is_audio_clip:
                raise ValueError("Clip is not an audio clip")
            clip.warping = bool(warping)
            if warp_mode is not None:
                clip.warp_mode = int(warp_mode)
            return {"clip_name": clip.name, "warping": clip.warping,
                    "warp_mode": clip.warp_mode}
        except Exception as e:
            self.log_message("Error setting clip warp: " + str(e))
            raise

    # ── Scenes ────────────────────────────────────────────────────────────────

    def _create_scene(self, index):
        """Create a new scene at the given index (-1 = end)."""
        try:
            self._song.create_scene(index)
            new_index = len(self._song.scenes) - 1 if index == -1 else index
            scene = self._song.scenes[new_index]
            return {"index": new_index, "name": scene.name}
        except Exception as e:
            self.log_message("Error creating scene: " + str(e))
            raise

    def _delete_scene(self, scene_index):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.delete_scene(scene_index)
            return {"deleted_index": scene_index, "scene_count": len(self._song.scenes)}
        except Exception as e:
            self.log_message("Error deleting scene: " + str(e))
            raise

    def _duplicate_scene(self, scene_index):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.duplicate_scene(scene_index)
            new_index = scene_index + 1
            return {"index": new_index, "name": self._song.scenes[new_index].name}
        except Exception as e:
            self.log_message("Error duplicating scene: " + str(e))
            raise

    def _fire_scene(self, scene_index):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.scenes[scene_index].fire()
            return {"fired_index": scene_index}
        except Exception as e:
            self.log_message("Error firing scene: " + str(e))
            raise

    def _set_scene_name(self, scene_index, name):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.scenes[scene_index].name = name
            return {"index": scene_index, "name": self._song.scenes[scene_index].name}
        except Exception as e:
            self.log_message("Error setting scene name: " + str(e))
            raise

    # ── Clip management ───────────────────────────────────────────────────────

    def _delete_clip(self, track_index, clip_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise Exception("No clip in slot")
            name = slot.clip.name
            slot.delete_clip()
            return {"deleted_name": name, "track_index": track_index,
                    "clip_index": clip_index}
        except Exception as e:
            self.log_message("Error deleting clip: " + str(e))
            raise

    def _duplicate_clip(self, track_index, clip_index, target_clip_index):
        """Duplicate a Session clip to another slot on the same track.

        target_clip_index=None picks the next empty slot after the source.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            src = track.clip_slots[clip_index]
            if not src.has_clip:
                raise Exception("No clip in source slot")
            if target_clip_index is None:
                for i in range(clip_index + 1, len(track.clip_slots)):
                    if not track.clip_slots[i].has_clip:
                        target_clip_index = i
                        break
                if target_clip_index is None:
                    raise Exception("No empty slot available after the source clip")
            if target_clip_index < 0 or target_clip_index >= len(track.clip_slots):
                raise IndexError("Target clip index out of range")
            dst = track.clip_slots[target_clip_index]
            src.duplicate_clip_to(dst)
            return {"source_index": clip_index, "target_index": target_clip_index,
                    "name": dst.clip.name if dst.has_clip else None}
        except Exception as e:
            self.log_message("Error duplicating clip: " + str(e))
            raise

    # ── Track routing ─────────────────────────────────────────────────────────

    def _routing_names(self, options):
        return [getattr(o, "display_name", str(o)) for o in options]

    def _get_track_routing(self, track_index):
        try:
            track = self._get_track_by_index(track_index, "regular")
            info = {"track_name": track.name}
            for attr in ("input_routing_type", "output_routing_type",
                         "input_routing_channel", "output_routing_channel"):
                cur = getattr(track, attr, None)
                info[attr] = getattr(cur, "display_name", None) if cur is not None else None
            for attr in ("available_input_routing_types", "available_output_routing_types",
                         "available_input_routing_channels", "available_output_routing_channels"):
                info[attr] = self._routing_names(getattr(track, attr, []))
            return info
        except Exception as e:
            self.log_message("Error getting track routing: " + str(e))
            raise

    def _apply_routing(self, track, kind, name):
        options = getattr(track, "available_" + kind + "s", [])
        for opt in options:
            if getattr(opt, "display_name", None) == name:
                setattr(track, kind, opt)
                return
        raise ValueError("Routing '%s' not found for %s. Available: %s"
                         % (name, kind, self._routing_names(options)))

    def _set_track_input_routing(self, track_index, routing_name, channel):
        try:
            track = self._get_track_by_index(track_index, "regular")
            if routing_name is not None:
                self._apply_routing(track, "input_routing_type", routing_name)
            if channel is not None:
                self._apply_routing(track, "input_routing_channel", channel)
            cur = track.input_routing_type
            ch = track.input_routing_channel
            return {"track_name": track.name,
                    "input_routing_type": getattr(cur, "display_name", None),
                    "input_routing_channel": getattr(ch, "display_name", None)}
        except Exception as e:
            self.log_message("Error setting input routing: " + str(e))
            raise

    def _set_track_output_routing(self, track_index, routing_name, channel):
        try:
            track = self._get_track_by_index(track_index, "regular")
            if routing_name is not None:
                self._apply_routing(track, "output_routing_type", routing_name)
            if channel is not None:
                self._apply_routing(track, "output_routing_channel", channel)
            cur = track.output_routing_type
            ch = track.output_routing_channel
            return {"track_name": track.name,
                    "output_routing_type": getattr(cur, "display_name", None),
                    "output_routing_channel": getattr(ch, "display_name", None)}
        except Exception as e:
            self.log_message("Error setting output routing: " + str(e))
            raise

    # ── Clip automation envelopes ─────────────────────────────────────────────

    def _write_envelope(self, clip, param, points, clear_existing):
        """Write a staircase automation envelope for ``param`` inside ``clip``.

        insert_step writes a flat segment [time, time+duration]; a zero-length
        step spans the whole clip, so each point holds its value until the next
        (a staircase). Callers can override a point's span with "duration".
        """
        # Clearing is a Clip method (the envelope object has no clear()).
        if clear_existing:
            try:
                clip.clear_envelope(param)
            except Exception:
                pass
        # automation_envelope returns None when the clip has no envelope yet;
        # create_automation_envelope makes one.
        env = clip.automation_envelope(param)
        if env is None and hasattr(clip, "create_automation_envelope"):
            env = clip.create_automation_envelope(param)
        if env is None:
            raise ValueError("Could not create automation envelope for '%s' "
                             "(parameter may not be automatable)" % param.name)
        pts = sorted(points, key=lambda p: float(p["time"]))
        n = len(pts)
        for i, pt in enumerate(pts):
            t = float(pt["time"])
            v = max(param.min, min(param.max, float(pt["value"])))
            if "duration" in pt:
                dur = float(pt["duration"])
            elif i + 1 < n:
                dur = float(pts[i + 1]["time"]) - t
            else:
                dur = (t - float(pts[i - 1]["time"])) if n > 1 else 1.0
            env.insert_step(t, max(0.0, dur), v)
        # Sample just inside each segment; value_at_time at an exact segment
        # boundary returns the preceding segment's value.
        sampled = []
        for pt in pts:
            try:
                sampled.append({"time": pt["time"],
                                "value": env.value_at_time(float(pt["time"]) + 0.001)})
            except Exception:
                pass
        return {"point_count": len(points), "sampled": sampled}

    def _mixer_param(self, track, target, send_index):
        """Resolve a mixer DeviceParameter: 'volume', 'pan', or a send."""
        md = track.mixer_device
        if target == "volume":
            return md.volume
        elif target in ("pan", "panning"):
            return md.panning
        elif target == "send":
            sends = md.sends
            if send_index < 0 or send_index >= len(sends):
                raise IndexError("Send index out of range")
            return sends[send_index]
        raise ValueError("target must be 'volume', 'pan', or 'send'")

    def _set_clip_envelope(self, track_index, clip_index, device_index, points,
                           parameter_index=None, parameter_name=None,
                           clear_existing=True, chain_index=None,
                           chain_device_index=None):
        """Write automation points for a track device's parameter inside a clip.

        points: list of {"time": beats, "value": param value, "duration": beats}.
        """
        try:
            clip = self._get_clip(track_index, clip_index)
            track = self._song.tracks[track_index]
            device = self._resolve_device(track, device_index, chain_index, chain_device_index)
            param = self._resolve_param_on_device(device, parameter_index, parameter_name)
            info = self._write_envelope(clip, param, points, clear_existing)
            info.update({"clip_name": clip.name, "device_name": device.name,
                         "parameter_name": param.name})
            return info
        except Exception as e:
            self.log_message("Error setting clip envelope: " + str(e))
            raise

    def _set_clip_mixer_envelope(self, track_index, clip_index, target, points,
                                 send_index=0, clear_existing=True):
        """Automate a track's mixer parameter (volume/pan/send) inside a clip."""
        try:
            clip = self._get_clip(track_index, clip_index)
            track = self._song.tracks[track_index]
            param = self._mixer_param(track, target, send_index)
            info = self._write_envelope(clip, param, points, clear_existing)
            info.update({"clip_name": clip.name, "target": target,
                         "parameter_name": param.name})
            return info
        except Exception as e:
            self.log_message("Error setting clip mixer envelope: " + str(e))
            raise

    def _clear_clip_envelope(self, track_index, clip_index, device_index,
                             parameter_index=None, parameter_name=None,
                             chain_index=None, chain_device_index=None):
        try:
            clip = self._get_clip(track_index, clip_index)
            track = self._song.tracks[track_index]
            device = self._resolve_device(track, device_index, chain_index, chain_device_index)
            param = self._resolve_param_on_device(device, parameter_index, parameter_name)
            cleared = False
            try:
                clip.clear_envelope(param)
                cleared = True
            except Exception:
                pass
            return {"clip_name": clip.name, "parameter_name": param.name,
                    "cleared": cleared}
        except Exception as e:
            self.log_message("Error clearing clip envelope: " + str(e))
            raise

    # ── Transport / edit history ──────────────────────────────────────────────

    def _undo(self):
        try:
            if self._song.can_undo:
                self._song.undo()
            return {"can_undo": self._song.can_undo, "can_redo": self._song.can_redo}
        except Exception as e:
            self.log_message("Error during undo: " + str(e))
            raise

    def _redo(self):
        try:
            if self._song.can_redo:
                self._song.redo()
            return {"can_undo": self._song.can_undo, "can_redo": self._song.can_redo}
        except Exception as e:
            self.log_message("Error during redo: " + str(e))
            raise

    def _capture_midi(self):
        try:
            self._song.capture_midi()
            return {"captured": True}
        except Exception as e:
            self.log_message("Error capturing MIDI: " + str(e))
            raise

    # ── Colors, recording/transport, selection & view ─────────────────────────

    def _set_track_color(self, track_index, color, track_type="regular"):
        try:
            track = self._get_track_by_index(track_index, track_type)
            track.color = int(color)
            return {"name": track.name, "color": track.color}
        except Exception as e:
            self.log_message("Error setting track color: " + str(e))
            raise

    def _set_clip_color(self, track_index, clip_index, color):
        try:
            clip = self._get_clip(track_index, clip_index)
            clip.color = int(color)
            return {"clip_name": clip.name, "color": clip.color}
        except Exception as e:
            self.log_message("Error setting clip color: " + str(e))
            raise

    def _set_scene_color(self, scene_index, color):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            scene = self._song.scenes[scene_index]
            scene.color = int(color)
            return {"index": scene_index, "color": scene.color}
        except Exception as e:
            self.log_message("Error setting scene color: " + str(e))
            raise

    def _set_metronome(self, on):
        try:
            self._song.metronome = bool(on)
            return {"metronome": bool(self._song.metronome)}
        except Exception as e:
            self.log_message("Error setting metronome: " + str(e))
            raise

    def _set_arrangement_record(self, on):
        try:
            # record_mode updates on the next tick, so report the requested state
            # rather than an immediately-stale read-back.
            self._song.record_mode = 1 if on else 0
            return {"record_mode": 1 if on else 0}
        except Exception as e:
            self.log_message("Error setting arrangement record: " + str(e))
            raise

    def _set_session_record(self, on):
        try:
            self._song.session_record = bool(on)
            return {"session_record": bool(self._song.session_record)}
        except Exception as e:
            self.log_message("Error setting session record: " + str(e))
            raise

    def _set_arrangement_loop(self, enabled, start=None, length=None):
        try:
            self._song.loop = bool(enabled)
            if start is not None:
                self._song.loop_start = float(start)
            if length is not None:
                self._song.loop_length = float(length)
            return {"loop": bool(self._song.loop), "loop_start": self._song.loop_start,
                    "loop_length": self._song.loop_length}
        except Exception as e:
            self.log_message("Error setting arrangement loop: " + str(e))
            raise

    def _set_time_signature(self, numerator, denominator):
        try:
            self._song.signature_numerator = int(numerator)
            self._song.signature_denominator = int(denominator)
            return {"numerator": self._song.signature_numerator,
                    "denominator": self._song.signature_denominator}
        except Exception as e:
            self.log_message("Error setting time signature: " + str(e))
            raise

    def _set_track_fold(self, track_index, folded):
        try:
            track = self._get_track_by_index(track_index, "regular")
            if not getattr(track, "is_foldable", False):
                raise ValueError("Track %d is not a group/foldable track" % track_index)
            track.fold_state = 1 if folded else 0
            return {"name": track.name, "fold_state": int(track.fold_state)}
        except Exception as e:
            self.log_message("Error setting track fold: " + str(e))
            raise

    def _select_track(self, track_index, track_type="regular"):
        try:
            track = self._get_track_by_index(track_index, track_type)
            self._song.view.selected_track = track
            return {"selected_track": track.name}
        except Exception as e:
            self.log_message("Error selecting track: " + str(e))
            raise

    def _select_scene(self, scene_index):
        try:
            if scene_index < 0 or scene_index >= len(self._song.scenes):
                raise IndexError("Scene index out of range")
            self._song.view.selected_scene = self._song.scenes[scene_index]
            return {"selected_scene_index": scene_index}
        except Exception as e:
            self.log_message("Error selecting scene: " + str(e))
            raise

    def _show_view(self, view):
        try:
            self.application().view.show_view(view)
            return {"view": view}
        except Exception as e:
            self.log_message("Error showing view: " + str(e))
            raise

    # ── State observers ───────────────────────────────────────────────────────
    #
    # Live listeners fire on the main thread and push change events into a
    # buffer; the client drains them with poll_events. This keeps the socket
    # strictly request/response (no unsolicited pushes to demultiplex).

    OBSERVER_TARGETS = ("transport", "selection", "tracks", "scenes", "detail_clip",
                        "playing_slots", "track:<index>")

    def _push_event(self, ev):
        with self._event_lock:
            self._event_seq += 1
            ev["seq"] = self._event_seq
            ev["time"] = time.time()
            self._event_queue.append(ev)
            if len(self._event_queue) > 500:
                # Drop oldest so a client that stops polling can't grow it forever.
                self._event_queue = self._event_queue[-500:]

    def _add_listener(self, target, obj, name, fn):
        """Register a Live listener, wrapping it so a callback error can't break
        Live, and remembering how to remove it later."""
        def safe():
            try:
                fn()
            except Exception as e:
                self.log_message("Observer '%s' callback error: %s" % (name, str(e)))
        getattr(obj, "add_%s_listener" % name)(safe)
        self._subscriptions.setdefault(target, []).append(
            (obj, "remove_%s_listener" % name, safe))

    def _track_locator(self, track):
        for i, t in enumerate(self._song.tracks):
            if t == track:
                return i, "regular"
        for i, t in enumerate(self._song.return_tracks):
            if t == track:
                return i, "return"
        if track == self._song.master_track:
            return -1, "master"
        return -1, "unknown"

    def _subscribe(self, targets):
        song = self._song
        view = song.view
        added = []
        for target in targets:
            if target in self._subscriptions:
                continue  # already active
            if target == "transport":
                self._add_listener(target, song, "is_playing",
                                   lambda: self._push_event({"type": "is_playing", "value": song.is_playing}))
                self._add_listener(target, song, "tempo",
                                   lambda: self._push_event({"type": "tempo", "value": song.tempo}))
                self._add_listener(target, song, "metronome",
                                   lambda: self._push_event({"type": "metronome", "value": bool(song.metronome)}))
                self._add_listener(target, song, "loop",
                                   lambda: self._push_event({"type": "loop", "value": bool(song.loop)}))
                self._add_listener(target, song, "signature_numerator",
                                   lambda: self._push_event({"type": "signature",
                                                             "numerator": song.signature_numerator,
                                                             "denominator": song.signature_denominator}))
            elif target == "selection":
                self._add_listener(target, view, "selected_track", self._on_selected_track)
                self._add_listener(target, view, "selected_scene", self._on_selected_scene)
            elif target == "tracks":
                self._add_listener(target, song, "tracks",
                                   lambda: self._push_event({"type": "tracks", "count": len(song.tracks)}))
            elif target == "scenes":
                self._add_listener(target, song, "scenes",
                                   lambda: self._push_event({"type": "scenes", "count": len(song.scenes)}))
            elif target == "detail_clip":
                self._add_listener(target, view, "detail_clip", self._on_detail_clip)
            elif target == "playing_slots":
                # Session clip play/queue state for every current regular track.
                for i, tr in enumerate(song.tracks):
                    self._add_track_slot_listeners(target, tr, i)
            elif target.startswith("track:"):
                try:
                    idx = int(target.split(":", 1)[1])
                except (ValueError, IndexError):
                    raise ValueError("Bad track target '%s' (use 'track:<index>')" % target)
                if idx < 0 or idx >= len(song.tracks):
                    raise IndexError("Track index out of range for '%s'" % target)
                self._add_track_listeners(target, song.tracks[idx], idx)
            else:
                raise ValueError("Unknown observer target '%s' (valid: %s)"
                                 % (target, ", ".join(self.OBSERVER_TARGETS)))
            added.append(target)
        return {"subscribed": added, "active": list(self._subscriptions.keys())}

    def _add_track_listeners(self, target, track, req_index):
        """Observe a track's name, mixer state, and session play/queue slots.

        Listeners bind to the track object, so they keep working if the track is
        reordered; each event reports the track's current index."""
        def emit(change, value=None):
            cur, ttype = self._track_locator(track)
            ev = {"type": "track", "requested_index": req_index, "index": cur,
                  "name": track.name, "change": change}
            if value is not None:
                ev["value"] = value
            self._push_event(ev)
        self._add_listener(target, track, "name", lambda: emit("name"))
        self._add_listener(target, track, "mute", lambda: emit("mute", track.mute))
        self._add_listener(target, track, "solo", lambda: emit("solo", track.solo))
        if getattr(track, "can_be_armed", False):
            self._add_listener(target, track, "arm", lambda: emit("arm", track.arm))
        md = track.mixer_device
        self._add_listener(target, md.volume, "value", lambda: emit("volume", md.volume.value))
        self._add_listener(target, md.panning, "value", lambda: emit("panning", md.panning.value))
        self._add_listener(target, track, "playing_slot_index",
                           lambda: emit("playing_slot", track.playing_slot_index))
        self._add_listener(target, track, "fired_slot_index",
                           lambda: emit("fired_slot", track.fired_slot_index))

    def _add_track_slot_listeners(self, target, track, req_index):
        def emit(change, value):
            cur, ttype = self._track_locator(track)
            self._push_event({"type": "playing_slot", "requested_index": req_index,
                              "index": cur, "name": track.name,
                              "change": change, "value": value})
        self._add_listener(target, track, "playing_slot_index",
                           lambda: emit("playing_slot", track.playing_slot_index))
        self._add_listener(target, track, "fired_slot_index",
                           lambda: emit("fired_slot", track.fired_slot_index))

    def _on_selected_track(self):
        tr = self._song.view.selected_track
        idx, ttype = self._track_locator(tr)
        self._push_event({"type": "selected_track", "name": tr.name,
                          "index": idx, "track_type": ttype})

    def _on_selected_scene(self):
        sc = self._song.view.selected_scene
        idx = -1
        for i, s in enumerate(self._song.scenes):
            if s == sc:
                idx = i
                break
        self._push_event({"type": "selected_scene", "index": idx})

    def _on_detail_clip(self):
        clip = self._song.view.detail_clip
        self._push_event({"type": "detail_clip",
                          "name": clip.name if clip else None})

    def _unsubscribe(self, targets):
        removed = []
        for target in targets:
            subs = self._subscriptions.pop(target, None)
            if subs:
                for (obj, remove_name, cb) in subs:
                    try:
                        getattr(obj, remove_name)(cb)
                    except Exception:
                        pass
                removed.append(target)
        return {"unsubscribed": removed, "active": list(self._subscriptions.keys())}

    def _unsubscribe_all(self):
        return self._unsubscribe(list(self._subscriptions.keys()))

    def _poll_events(self, max_events=100, clear=True):
        with self._event_lock:
            evs = self._event_queue[:max_events]
            if clear:
                self._event_queue = self._event_queue[len(evs):]
            remaining = len(self._event_queue)
        return {"events": evs, "returned": len(evs), "remaining": remaining,
                "active_subscriptions": list(self._subscriptions.keys())}

    def _list_subscriptions(self):
        return {"active": list(self._subscriptions.keys()),
                "available": list(self.OBSERVER_TARGETS),
                "buffered_events": len(self._event_queue)}


    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. If no marker
    # matches we fall back to the default order, so this is purely an
    # optimisation — never a correctness change.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"
    
    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # Helper function to process a browser item and its children
            def process_item(item, depth=0):
                if not item:
                    return None
                
                result = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": hasattr(item, 'children') and bool(item.children),
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }
                
                
                return result
            
            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
