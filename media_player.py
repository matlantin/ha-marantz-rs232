"""Media player platform for Marantz SR6001 via RS232.

This implementation is a minimal, safe scaffold. It sends raw strings to
the serial port and expects the user to provide the exact RS232 commands
for the SR6001 (see README for where to add them).

Notes for the user:
- You must install this folder under Home Assistant's `custom_components`.
- Add configuration to `configuration.yaml` (example in README).
- After restarting Home Assistant, an entity will be created and can be
  controlled from the UI or developer services.
"""

import asyncio
import logging
import time
import threading
from typing import Optional

import serial
import os
from pathlib import Path
import yaml
import re

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    PLATFORM_SCHEMA,
    MediaPlayerEntityFeature,
)
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta
import voluptuous as vol

from .const import DOMAIN, DEFAULT_BAUDRATE, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)


def _read_yaml_file(path: Path):
    """Blocking helper to read a YAML file (run in executor)."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _write_yaml_file(path: Path, data: dict):
    """Blocking helper to write a YAML file (run in executor)."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)


class _SupportedFeaturesWrapper(int):
    """Int-like wrapper that supports membership checks.

    Subclassing `int` makes instances JSON-serializable (Home Assistant needs
    `supported_features` to be a primitive for websocket serialization). We
    still implement `__contains__` so HA code that does `if FLAG in
    entity.supported_features` works as expected.
    """

    def __new__(cls, mask: int):
        return int.__new__(cls, int(mask))

    def __contains__(self, item):
        # item may be an Enum with .value or an int
        try:
            val = int(item.value)
        except Exception:
            try:
                val = int(item)
            except Exception:
                return False
        return bool(int(self) & val)

    def __repr__(self):
        return f"_SupportedFeaturesWrapper({int(self)})"


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required("serial_port"): cv.string,
        vol.Optional("baudrate", default=DEFAULT_BAUDRATE): cv.positive_int,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional("command_map", default={}): dict,
        vol.Optional("poll_interval", default=10): cv.positive_int,
        vol.Optional("optimistic", default=False): cv.boolean,
        vol.Optional("use_marantzusb_format", default=True): cv.boolean,
        # command_map is a dictionary where keys are logical actions
        # (e.g. 'power_on', 'power_off', 'volume_up', 'query_volume')
        # and values are the raw strings to send to the RS232 port.
    }
)


class SerialDevice:
    """Simple blocking serial helper (used in executor).

    It opens the port on demand and provides `write_raw` and `query`.
    Includes robust timeout handling and automatic reconnection.
    """

    # Global timeout to avoid indefinite blocking
    OPERATION_TIMEOUT = 3.0  # seconds
    MAX_RECONNECT_ATTEMPTS = 2

    def __init__(self, port: str, baudrate: int, newline: str = "\r"):
        self.port = port
        self.baudrate = baudrate
        self.newline = newline
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()  # Protects serial operations
        self._consecutive_errors = 0
        self._last_successful_op = time.time()

    def open(self) -> bool:
        """Open serial port with timeout protection. Returns True if successful."""
        try:
            if self._serial is not None and self._serial.is_open:
                return True
            # Cleanly close if in an inconsistent state
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
            # Open with a short read timeout
            self._serial = serial.Serial(
                self.port, 
                self.baudrate, 
                timeout=0.5,  # Short read timeout
                write_timeout=1.0  # Write timeout
            )
            self._consecutive_errors = 0
            _LOGGER.debug("Serial port %s opened successfully", self.port)
            return True
        except Exception as exc:
            _LOGGER.error("Unable to open serial port %s: %s", self.port, exc)
            self._serial = None
            return False

    def close(self):
        """Close serial port safely."""
        try:
            if self._serial is not None:
                if self._serial.is_open:
                    self._serial.close()
                self._serial = None
        except Exception as exc:
            _LOGGER.debug("Error closing serial port: %s", exc)
            self._serial = None

    def _reset_port(self) -> bool:
        """Force reset of the serial port. Returns True if successful."""
        _LOGGER.warning("Resetting serial port %s...", self.port)
        self.close()
        time.sleep(0.1)
        return self.open()

    def write_raw(self, raw: str, timeout: float = None) -> bool:
        """Write command to serial port with timeout protection. Returns True if successful."""
        if timeout is None:
            timeout = self.OPERATION_TIMEOUT
        
        if not self._lock.acquire(timeout=timeout):
            _LOGGER.warning("write_raw: unable to acquire lock (timeout)")
            return False
        
        try:
            start_time = time.time()
            
            # Ensure start character '@' is present per spec
            if not raw.startswith("@"):
                raw = "@" + raw
            payload = raw.encode("ascii", errors="ignore") + self.newline.encode()
            
            for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
                if time.time() - start_time > timeout:
                    _LOGGER.error("write_raw: global timeout exceeded")
                    return False
                
                if not self.open():
                    time.sleep(0.1)
                    continue
                
                try:
                    _LOGGER.debug("SerialDevice.write_raw: port=%s command=%s", self.port, raw)
                    self._serial.write(payload)
                    self._serial.flush()  # Ensure data is sent
                    time.sleep(0.08)
                    self._consecutive_errors = 0
                    self._last_successful_op = time.time()
                    return True
                except serial.SerialTimeoutException:
                    _LOGGER.warning("write_raw: write timeout, attempt %d/%d", attempt + 1, self.MAX_RECONNECT_ATTEMPTS)
                    self._reset_port()
                except Exception as exc:
                    _LOGGER.error("write_raw error: %s, attempt %d/%d", exc, attempt + 1, self.MAX_RECONNECT_ATTEMPTS)
                    self._consecutive_errors += 1
                    self._reset_port()
            
            _LOGGER.error("write_raw: failed after %d attempts", self.MAX_RECONNECT_ATTEMPTS)
            return False
        finally:
            self._lock.release()

    def query(self, raw: str, timeout: float = None) -> str:
        """Send a command and return the raw response as text with timeout protection."""
        if timeout is None:
            timeout = self.OPERATION_TIMEOUT
        
        if not self._lock.acquire(timeout=timeout):
            _LOGGER.warning("query: unable to acquire lock (timeout)")
            return ""
        
        try:
            start_time = time.time()
            
            for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    _LOGGER.error("query: global timeout exceeded")
                    return ""
                
                if not self.open():
                    time.sleep(0.1)
                    continue
                
                try:
                    # Clear input buffer
                    self._serial.reset_input_buffer()
                    
                    payload = raw.encode("ascii", errors="ignore") + self.newline.encode()
                    self._serial.write(payload)
                    self._serial.flush()
                    time.sleep(0.12)
                    
                    # Read response with strict timeout
                    read_deadline = time.time() + min(0.8, remaining - 0.2)
                    buf = bytearray()
                    
                    while time.time() < read_deadline:
                        try:
                            # Check if data is available
                            if self._serial.in_waiting > 0:
                                chunk = self._serial.read(min(self._serial.in_waiting, 1024))
                            else:
                                chunk = self._serial.read_until(self.newline.encode(), size=256)
                            
                            if chunk:
                                buf.extend(chunk)
                                # If terminator received, we can exit
                                if self.newline.encode() in chunk:
                                    break
                                time.sleep(0.02)
                            else:
                                # No data, exit loop
                                break
                        except serial.SerialException as exc:
                            _LOGGER.debug("query: read error: %s", exc)
                            break
                    
                    if buf:
                        try:
                            text = buf.decode("ascii", errors="ignore")
                            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                            _LOGGER.debug("SerialDevice.query: cmd=%s response=%s", raw, repr(text))
                            self._consecutive_errors = 0
                            self._last_successful_op = time.time()
                            return text
                        except Exception:
                            pass
                    
                    # No response, but not necessarily an error
                    _LOGGER.debug("query: no response for %s", raw)
                    return ""
                    
                except serial.SerialTimeoutException:
                    _LOGGER.warning("query: timeout, attempt %d/%d", attempt + 1, self.MAX_RECONNECT_ATTEMPTS)
                    self._reset_port()
                except Exception as exc:
                    _LOGGER.error("query error: %s, attempt %d/%d", exc, attempt + 1, self.MAX_RECONNECT_ATTEMPTS)
                    self._consecutive_errors += 1
                    self._reset_port()
            
            return ""
        finally:
            self._lock.release()

    def is_healthy(self) -> bool:
        """Check if the serial connection appears healthy."""
        if self._consecutive_errors >= 3:
            return False
        if time.time() - self._last_successful_op > 60:
            # No successful operation for 60 seconds
            return False
        return True


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Marantz RS232 media player from a config entry."""
    config = entry.data
    name = config.get(CONF_NAME)
    port = config.get("serial_port")
    baud = config.get("baudrate")
    
    # Defaults for UI config
    command_map = {} 
    optimistic = False
    use_marantzusb_format = True
    poll_interval = 10

    await _async_common_setup(hass, name, port, baud, command_map, optimistic, use_marantzusb_format, poll_interval, async_add_entities)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Marantz RS232 media player from YAML configuration."""
    name = config.get(CONF_NAME)
    port = config["serial_port"]
    baud = config.get("baudrate")
    command_map = config.get("command_map", {}) or {}
    optimistic = config.get("optimistic", False)
    use_marantzusb_format = config.get("use_marantzusb_format", False)
    poll_interval = config.get("poll_interval", 10)

    await _async_common_setup(hass, name, port, baud, command_map, optimistic, use_marantzusb_format, poll_interval, async_add_entities)


async def _async_common_setup(hass, name, port, baud, command_map, optimistic, use_marantzusb_format, poll_interval, async_add_entities):
    """Shared setup logic for YAML and Config Entry."""
    # If no command_map provided in configuration, try to load the parsed YAML
    if not command_map:
        try:
            base = Path(__file__).parent
            yaml_path = base / "command_map_parsed.yaml"
            if yaml_path.exists():
                try:
                    data = await hass.async_add_executor_job(_read_yaml_file, yaml_path)
                    # Merge top-level keys into command_map
                    # Keep 'sources' nested separately
                    for k, v in data.items():
                        if k == "sources":
                            command_map.setdefault("sources", {}).update(v or {})
                        else:
                            # allow values like 'SRC:1' without '@'
                            command_map.setdefault(k, v)
                    _LOGGER.info("Loaded default command_map from %s", yaml_path)
                except Exception:  # pragma: no cover - best effort loading
                    _LOGGER.exception("Failed to load default command_map_parsed.yaml")
        except Exception:  # pragma: no cover - best effort loading
            _LOGGER.exception("Failed to load default command_map_parsed.yaml")

    device = SerialDevice(port, baud)
    # Track parsed yaml path so entity can persist automatic mappings
    parsed_yaml_path = None
    try:
        base = Path(__file__).parent
        yaml_path = base / "command_map_parsed.yaml"
        if yaml_path.exists():
            parsed_yaml_path = yaml_path
    except Exception:
        parsed_yaml_path = None

    entity = MarantzRS6001(
        device,
        name,
        command_map,
        optimistic=optimistic,
        parsed_yaml_path=parsed_yaml_path,
        use_marantzusb_format=use_marantzusb_format,
    )
    async_add_entities([entity], update_before_add=True)

    # Ensure an initial HA state is written after adding the entity so it
    # doesn't appear as 'unknown' while the first scheduled poll runs.
    try:
        await entity.async_update()
        if getattr(entity, "entity_id", None):
            entity.async_write_ha_state()
    except Exception:
        # Best-effort: don't prevent platform setup if initial update fails
        _LOGGER.debug("Initial entity update failed (non-fatal)")

    # Setup periodic polling if requested
    try:
        if poll_interval and int(poll_interval) > 0:
            interval = timedelta(seconds=int(poll_interval))

            async def _async_poll(now):
                try:
                    # Check if we should skip this poll (recent user command)
                    if time.time() < entity._skip_poll_until:
                        _LOGGER.debug("Polling skipped: user command in progress")
                        return
                    
                    # Check serial port health
                    if not device.is_healthy():
                        _LOGGER.warning("Serial port unhealthy, attempting reset")
                        await hass.async_add_executor_job(device._reset_port)
                    
                    # Polling with timeout to avoid blocking
                    try:
                        await asyncio.wait_for(entity.async_update(), timeout=8.0)
                    except asyncio.TimeoutError:
                        _LOGGER.warning("Polling timeout after 8 seconds - serial port might be blocked")
                        # Force port reset
                        await hass.async_add_executor_job(device._reset_port)
                        return
                    
                    # Only write HA state if the entity has been assigned an entity_id
                    # (avoids NoEntitySpecifiedError when the platform/setup timing
                    # results in update running before the entity is fully registered).
                    if getattr(entity, "entity_id", None):
                        entity.async_write_ha_state()
                except Exception:  # pragma: no cover - runtime
                    _LOGGER.exception("Error during scheduled poll of Marantz entity")

            async_track_time_interval(hass, _async_poll, interval)
            _LOGGER.info("Scheduled Marantz polling every %s seconds", poll_interval)
    except Exception:
        _LOGGER.exception("Failed to schedule Marantz polling interval")

    # Register a simple service to send arbitrary raw RS232 commands for debugging.
    # Usage from Developer Tools -> Services:
    # service: marantz_rs232.send_raw
    # service data: {"command": "@PWR:2", "entity_id": "media_player.marantz_sr6001"}
    async def handle_send_raw(call):
        cmd = call.data.get("command")
        target = call.data.get("entity_id")
        if not cmd:
            _LOGGER.warning("marantz_rs232.send_raw called without 'command'")
            return
        # If entity_id provided, ensure it matches this entity
        if target:
            # allow list or single
            if isinstance(target, str) and target != entity.entity_id:
                _LOGGER.debug("send_raw target %s does not match entity %s; ignoring", target, entity.entity_id)
                return
        _LOGGER.debug("Sending raw command to Marantz: %s", cmd)
        # Use `query` to send and read response (query handles CR and reading)
        try:
            resp = await hass.async_add_executor_job(device.query, cmd)
        except Exception as exc:  # pragma: no cover - runtime
            _LOGGER.exception("Error sending raw command: %s", exc)
            resp = None

        _LOGGER.info("marantz_rs232.send_raw response for %s: %s", cmd, resp)
        # Fire an event so users can listen to responses in Developer Tools -> Events
        hass.bus.async_fire(
            "marantz_rs232_raw_response",
            {"entity_id": entity.entity_id, "command": cmd, "response": resp},
        )

    # Use a simple schema for service registration to avoid issues when Home Assistant
    # loads service metadata. Accept command as string and optional entity_id as string.
    schema = vol.Schema({vol.Required("command"): cv.string, vol.Optional("entity_id"): cv.string})

    hass.services.async_register(DOMAIN, "send_raw", handle_send_raw, schema=schema)



class MarantzRS6001(MediaPlayerEntity):

    async def async_mute_volume(self, mute: bool) -> None:
        """Enable or disable mute on the amplifier."""
        cmd = None
        if mute:
            cmd = self._cmd.get("mute_on")
        else:
            cmd = self._cmd.get("mute_off")
        if not cmd:
            _LOGGER.warning("No mute_on/mute_off command defined in command_map")
            return
        await self._write_opportunistic(cmd)
        # Optimistic update: show mute state immediately
        self._muted = mute
        self.async_write_ha_state()

    """Media player entity for a Marantz SR6001 (RS232).

    This entity sends commands defined in `command_map`. The mapping is
    intentionally provided by the user because exact RS232 strings depend on firmware/format.
    See README for examples and how to populate the map.
    """

    @property
    def extra_state_attributes(self):
        """Expose extra attributes for Home Assistant."""
        attrs = {}
        db = self.volume_db
        if db is not None:
            attrs["volume_db"] = db
        return attrs

    def __init__(self, device: SerialDevice, name: str, command_map: dict, optimistic: bool = False, parsed_yaml_path: Optional[Path] = None, use_marantzusb_format: bool = False):
        self._device = device
        self._name = name
        self._cmd = command_map or {}
        self._optimistic = optimistic
        self._parsed_yaml_path = parsed_yaml_path
        # If True, only send marantzusb-style absolute volume commands (e.g. '@VOL:0-23')
        self._use_marantzusb_format = bool(use_marantzusb_format)
        
        self._is_on = None
        self._volume_level = None  # 0..1
        self._muted = None
        self._source = None
        self._available = True
        # None = unknown, True/False = whether absolute volume set is supported
        self._absolute_supported = None
        # Debounce task and pending volume for slider movements
        self._debounce_task = None
        self._pending_volume = None
        # debounce interval in seconds for slider aggregation
        self._debounce_interval = 0.35
        
        # Polling control: avoid polluting during user commands
        self._user_command_in_progress = False
        self._last_user_command_time = 0.0
        self._skip_poll_until = 0.0  # Timestamp until which to skip polling
        # Flag to indicate an update is in progress (avoids parallel updates)
        self._update_in_progress = False
        # Volume lock: after a user change, keep the chosen value
        # for a few seconds without replacing it with the amplifier reading
        self._volume_locked_until = 0.0
        self._volume_lock_duration = 4.0  # seconds

        # Build a set of supported MediaPlayerEntityFeature flags so that
        # membership checks like "MediaPlayerEntityFeature.GROUPING in self.supported_features"
        # work (Home Assistant checks membership rather than bitwise ops in recent versions).
        try:
            self._supported_features_set = {
                MediaPlayerEntityFeature.TURN_ON,
                MediaPlayerEntityFeature.TURN_OFF,
                MediaPlayerEntityFeature.VOLUME_STEP,
                MediaPlayerEntityFeature.VOLUME_SET,
                MediaPlayerEntityFeature.VOLUME_MUTE,
                MediaPlayerEntityFeature.SELECT_SOURCE,
            }
        except Exception:
            # Fallback: empty set
            self._supported_features_set = set()

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self) -> Optional[str]:
        """Return a stable unique id for this entity.

        Use a deterministic combination of the configured name and the
        serial port path so the id remains stable across restarts on the
        same system.
        """
        try:
            name_part = re.sub(r"[^0-9a-zA-Z]+", "_", (self._name or "marantz")).strip("_").lower()
        except Exception:
            name_part = "marantz"
        try:
            port_part = re.sub(r"[^0-9a-zA-Z]+", "_", (getattr(self._device, "port", "unknown") or "unknown")).strip("_").lower()
        except Exception:
            port_part = "unknown"
        return f"marantz_rs232_{name_part}_{port_part}"

    @property
    def state(self):
        if self._is_on:
            return "on"
        if self._is_on is False:
            return "off"
        return None

    @property
    def volume_level(self):
        return self._volume_level

    @property
    def volume_db(self):
        """Return the exact volume dB value returned by the amplifier (if available)."""
        if hasattr(self, '_volume_db_exact') and self._volume_db_exact is not None:
            return self._volume_db_exact
        return None

    @property
    def is_volume_muted(self):
        return self._muted

    @property
    def source(self):
        return self._source

    @property
    def source_list(self):
        """Return available sources (for the UI dropdown)."""
        src_map = self._cmd.get("sources", {}) or {}
        # maintain order if mapping is ordered; return list of keys
        try:
            return list(src_map.keys())
        except Exception:
            return []

    @property
    def available(self):
        return self._available

    @property
    def supported_features(self):
        # Compute integer mask from MediaPlayerEntityFeature members and
        # return a wrapper that supports both bitwise ops and membership.
        mask = 0
        for feat in getattr(self, "_supported_features_set", []):
            try:
                mask |= int(feat.value)
            except Exception:
                try:
                    mask |= int(feat)
                except Exception:
                    pass
        return _SupportedFeaturesWrapper(mask)

    async def async_turn_on(self):
        cmd = self._cmd.get("power_on")
        if not cmd:
            _LOGGER.warning("No 'power_on' command provided in command_map")
            return
        await self._write_opportunistic(cmd, verify_cmd=self._cmd.get("query_power"))
        # refresh state after the command
        try:
            await asyncio.sleep(0.12)
            await self.async_update()
            try:
                self.async_write_ha_state()
            except Exception:
                pass
        except Exception:
            pass
        if self._optimistic:
            self._is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self):
        cmd = self._cmd.get("power_off")
        if not cmd:
            _LOGGER.warning("No 'power_off' command provided in command_map")
            return
        await self._write_opportunistic(cmd, verify_cmd=self._cmd.get("query_power"))
        # refresh state after the command
        try:
            await asyncio.sleep(0.12)
            await self.async_update()
            try:
                self.async_write_ha_state()
            except Exception:
                pass
        except Exception:
            pass
        if self._optimistic:
            self._is_on = False
            self.async_write_ha_state()

    async def async_volume_up(self):
        cmd = self._cmd.get("volume_up")
        if not cmd:
            _LOGGER.warning("No 'volume_up' command provided in command_map")
            return
        await self._write_opportunistic(cmd, verify_cmd=self._cmd.get("query_volume"))
        if self._optimistic:
            # optimistic approximate increase
            if self._volume_level is None:
                self._volume_level = 0.5
            else:
                self._volume_level = min(1.0, self._volume_level + 0.05)
            self.async_write_ha_state()

    async def async_volume_down(self):
        cmd = self._cmd.get("volume_down")
        if not cmd:
            _LOGGER.warning("No 'volume_down' command provided in command_map")
            return
        await self._write_opportunistic(cmd, verify_cmd=self._cmd.get("query_volume"))
        if self._optimistic:
            if self._volume_level is None:
                self._volume_level = 0.5
            else:
                self._volume_level = max(0.0, self._volume_level - 0.05)
            self.async_write_ha_state()

    async def async_select_source(self, source: str) -> None:
        """Select an input/source by friendly name (async).

        Looks up the command in `command_map['sources']` or formats a
        `select_source` template if provided.
        """
        if source is None:
            return
        src_map = self._cmd.get("sources", {}) or {}
        cmd = None
        if source in src_map:
            cmd = src_map.get(source)
        else:
            tmpl = self._cmd.get("select_source")
            if tmpl:
                try:
                    cmd = tmpl.format(source=source)
                except Exception:
                    cmd = tmpl.replace("{source}", str(source))

        if not cmd:
            _LOGGER.warning("No command found for source '%s'", source)
            return
        await self._write_opportunistic(cmd, verify_cmd=self._cmd.get("query_power") or self._cmd.get("query_source"))
        if self._optimistic:
            self._source = source
            try:
                self.async_write_ha_state()
            except Exception:
                pass

    def select_source(self, source: str) -> None:
        """Synchronous wrapper for `async_select_source` called by HA sync APIs."""
        try:
            fut = asyncio.run_coroutine_threadsafe(self.async_select_source(source), self.hass.loop)
            fut.result(timeout=10)
        except Exception as exc:  # pragma: no cover - runtime
            _LOGGER.exception("Error in select_source: %s", exc)

    async def _write_opportunistic(self, cmd: str, verify_cmd: Optional[str] = None) -> bool:
        """Send a command to the device with user-command tracking.

        The SerialDevice class handles its own thread-level locking, so we
        don't need an asyncio lock here. We just track that a user command
        is in progress to skip polling during this time.
        
        Returns True if the write was successful.
        """
        # Mark that a user command is in progress
        self._user_command_in_progress = True
        self._last_user_command_time = time.time()
        # Delay next polling by 3 seconds
        self._skip_poll_until = time.time() + 3.0
        
        success = False
        try:
            # SerialDevice.write_raw handles its own timeout and locking
            result = await self.hass.async_add_executor_job(self._device.write_raw, cmd)
            success = result if isinstance(result, bool) else True
            if not success:
                _LOGGER.warning("_write_opportunistic: failed to write %s", cmd)
        except Exception as exc:
            _LOGGER.error("_write_opportunistic: error writing: %s", exc)
            success = False
        finally:
            self._user_command_in_progress = False

        # Schedule a delayed verification if requested (fire and forget)
        if success and verify_cmd:
            async def _verify():
                await asyncio.sleep(0.3)
                try:
                    resp = await self.hass.async_add_executor_job(self._device.query, verify_cmd)
                    _LOGGER.debug("verify %s -> %s", verify_cmd, resp)
                except Exception:
                    pass

            asyncio.create_task(_verify())
        
        return success

    async def _apply_volume(self, volume: float) -> None:
        """Apply a volume change (called after debounce).
        
        SerialDevice handles its own thread-level locking, so we don't need
        an asyncio lock here.
        """
        if volume is None:
            return
        volume = max(0.0, min(1.0, float(volume)))
        
        # Mark that a user command is in progress
        self._user_command_in_progress = True
        self._skip_poll_until = time.time() + 3.0
        
        try:
            # Range used by many Marantz devices
            max_db = 18.0
            min_db = -80.0

            tmpl = self._cmd.get("volume_set_template") or self._cmd.get("volume_set")

            # Attempt absolute set if available and not previously marked unsupported
            if tmpl and (self._absolute_supported is not False):
                db = min_db + volume * (max_db - min_db)
                candidate_cmds = []
                try:
                    val_tenths = int(round(db * 10))
                except Exception:
                    val_tenths = int(round(db))
                if tmpl:
                    try:
                        tmpl_cmd = tmpl.format(value=val_tenths)
                    except Exception:
                        tmpl_cmd = tmpl.replace("{value}", str(val_tenths))
                else:
                    tmpl_cmd = None

                # marantzusb-style: prefix '0' then signed integer decibels
                marantzusb_cmd = None
                try:
                    db_int = int(round(db))
                    marantzusb_cmd = f"@VOL:0{db_int:+d}"
                except Exception:
                    marantzusb_cmd = None

                candidate_cmds = []
                if self._use_marantzusb_format:
                    if marantzusb_cmd:
                        candidate_cmds.append(marantzusb_cmd)
                else:
                    if tmpl_cmd:
                        candidate_cmds.append(tmpl_cmd)
                    if marantzusb_cmd:
                        candidate_cmds.append(marantzusb_cmd)

                _LOGGER.debug("Debounced: trying absolute volume candidate commands: %s", candidate_cmds)

                query_cmd = self._cmd.get("query_volume")
                new_level = None
                for cmd in candidate_cmds:
                    # Send command
                    result = await self.hass.async_add_executor_job(self._device.write_raw, cmd)
                    if not result:
                        continue

                    # verify
                    if query_cmd:
                        await asyncio.sleep(0.15)
                        try:
                            resp = await self.hass.async_add_executor_job(self._device.query, query_cmd)
                        except Exception:
                            resp = None
                        if resp:
                            m = re.search(r"VOL[: ]\s*([-+]?[0-9]+(?:\.[0-9]+)?)", resp)
                            if not m:
                                m2 = re.search(r"L[: ]\s*([0-9]+)", resp)
                                sval = m2.group(1) if m2 else None
                            else:
                                sval = m.group(1)
                            if sval is not None:
                                try:
                                    if re.fullmatch(r"\d{3}", sval):
                                        db2 = int(sval) / 10.0
                                    else:
                                        db2 = float(sval)
                                    db2 = max(min_db, min(max_db, db2))
                                    new_level = (db2 - min_db) / (max_db - min_db)
                                except Exception:
                                    new_level = None

                    if new_level is not None and abs(new_level - volume) <= 0.02:
                        self._absolute_supported = True
                        if self._optimistic:
                            self._volume_level = new_level
                            try:
                                self.async_write_ha_state()
                            except Exception:
                                pass
                        return  # Succès!

                if new_level is None:
                    self._absolute_supported = False
                    _LOGGER.debug("Debounced: absolute set did not take effect, falling back to step commands")

            # Fallback: compute steps in dB and send that many +/- commands
            up_cmd = self._cmd.get("volume_up")
            down_cmd = self._cmd.get("volume_down")
            if not up_cmd and not down_cmd:
                _LOGGER.warning("No volume step commands available; cannot apply volume change")
                return

            # Query current volume if possible
            current_level = self._volume_level
            query_cmd = self._cmd.get("query_volume")
            if query_cmd:
                try:
                    resp = await self.hass.async_add_executor_job(self._device.query, query_cmd)
                except Exception:
                    resp = None
                if resp:
                    m = re.search(r"VOL[: ]\s*([-+]?[0-9]+(?:\.[0-9]+)?)", resp)
                    if not m:
                        m2 = re.search(r"L[: ]\s*([0-9]+)", resp)
                        sval = m2.group(1) if m2 else None
                    else:
                        sval = m.group(1)
                    if sval is not None:
                        try:
                            if re.fullmatch(r"\d{3}", sval):
                                dbc = int(sval) / 10.0
                            else:
                                dbc = float(sval)
                            dbc = max(min_db, min(max_db, dbc))
                            current_level = (dbc - min_db) / (max_db - min_db)
                        except Exception:
                            pass

            if current_level is None:
                # just do one best-effort step
                await self.hass.async_add_executor_job(
                    self._device.write_raw, up_cmd if volume > 0.5 else down_cmd
                )
                return

            current_db = min_db + current_level * (max_db - min_db)
            target_db = min_db + volume * (max_db - min_db)
            delta_db = target_db - current_db
            steps = int(round(abs(delta_db)))
            if steps <= 0:
                return

            cmd = up_cmd if delta_db > 0 else down_cmd
            for _ in range(steps):
                await self.hass.async_add_executor_job(self._device.write_raw, cmd)
                await asyncio.sleep(0.08)

            # After applying steps, query once and update internal state
            if query_cmd:
                try:
                    resp = await self.hass.async_add_executor_job(self._device.query, query_cmd)
                except Exception:
                    resp = None
                if resp:
                    m = re.search(r"VOL[: ]\s*([-+]?[0-9]+(?:\.[0-9]+)?)", resp)
                    if not m:
                        m2 = re.search(r"L[: ]\s*([0-9]+)", resp)
                        sval = m2.group(1) if m2 else None
                    else:
                        sval = m.group(1)
                    if sval is not None:
                        try:
                            if re.fullmatch(r"\d{3}", sval):
                                dbc = int(sval) / 10.0
                            else:
                                dbc = float(sval)
                            dbc = max(min_db, min(max_db, dbc))
                            new_level = (dbc - min_db) / (max_db - min_db)
                            self._volume_level = new_level
                            if self._optimistic:
                                try:
                                    self.async_write_ha_state()
                                except Exception:
                                    pass
                        except Exception:
                            pass
        finally:
            self._user_command_in_progress = False

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume to a specific level (0..1).

        This method debounces rapid slider updates and schedules the actual
        application in `_apply_volume` to avoid flooding the serial port.
        """
        if volume is None:
            return
        volume = max(0.0, min(1.0, float(volume)))

        # Immediate UI update with chosen value
        # and lock to prevent polling from overwriting it
        self._volume_level = volume
        self._volume_locked_until = time.time() + self._volume_lock_duration
        try:
            self.async_write_ha_state()
        except Exception:
            pass

        # store pending target and debounce
        self._pending_volume = volume
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        async def _delayed_apply():
            try:
                await asyncio.sleep(self._debounce_interval)
                await self._apply_volume(self._pending_volume)
            except asyncio.CancelledError:
                return

        self._debounce_task = asyncio.create_task(_delayed_apply())

    async def async_update(self):
        """Poll the device for current power, volume and source state.
        
        SerialDevice handles its own thread-level locking, so we don't need
        an asyncio lock here. We just use _update_in_progress to avoid
        parallel updates.
        """
        # Check if we should skip this update (recent user command)
        if self._user_command_in_progress:
            _LOGGER.debug("async_update: skipped because user command in progress")
            return
        
        # Check if an update is already in progress
        if self._update_in_progress:
            _LOGGER.debug("async_update: skipped because update already in progress")
            return
        
        self._update_in_progress = True
        try:
            # QUERY POWER
            if "query_power" in self._cmd:
                try:
                    resp = await self.hass.async_add_executor_job(
                        self._device.query, self._cmd.get("query_power")
                    )
                except Exception:
                    resp = None
                _LOGGER.debug("query_power response: %s", resp)
                if resp:
                    m = re.search(r"PWR[: ]\s*([0-9A-Za-z]+)", resp, re.IGNORECASE)
                    if m:
                        code = m.group(1).strip()
                        try:
                            if code.isdigit():
                                v = int(code)
                                self._is_on = bool(v == 2)
                            else:
                                self._is_on = code.lower() in ("on", "1", "true")
                        except Exception:
                            self._is_on = None

            # QUERY VOLUME
            if "query_volume" in self._cmd:
                try:
                    resp = await self.hass.async_add_executor_job(
                        self._device.query, self._cmd.get("query_volume")
                    )
                except Exception:
                    resp = None
                _LOGGER.debug("query_volume response: %s", resp)
                if resp:
                    m = re.search(r"VOL[: ]\s*([-+]?[0-9]+(?:\.[0-9]+)?)", resp)
                    if not m:
                        m2 = re.search(r"L[: ]\s*([0-9]+)", resp)
                        sval = m2.group(1) if m2 else None
                    else:
                        sval = m.group(1)
                    if sval is not None:
                        try:
                            if re.fullmatch(r"\d{3}", sval):
                                db = int(sval) / 10.0
                            else:
                                db = float(sval)
                            self._volume_db_exact = db
                            # Only update level if volume is not locked
                            if time.time() >= self._volume_locked_until:
                                self._volume_level = (db - (-72.0)) / (15.0 - (-72.0))
                                self._volume_level = max(0.0, min(1.0, self._volume_level))
                                _LOGGER.debug("Parsed volume dB=%s -> level=%s", db, self._volume_level)
                            else:
                                _LOGGER.debug("Volume locked, read value ignored: dB=%s", db)
                        except Exception:
                            self._volume_db_exact = None

            # QUERY SOURCE
            if "query_source" in self._cmd:
                try:
                    resp = await self.hass.async_add_executor_job(
                        self._device.query, self._cmd.get("query_source")
                    )
                except Exception:
                    resp = None
                _LOGGER.debug("query_source response: %s", resp)
                if resp:
                    m = re.search(r"SRC[: ]\s*([0-9A-Za-z]+)", resp, re.IGNORECASE)
                    if m:
                        code = m.group(1).upper()
                        src_map = self._cmd.get("sources", {}) or {}
                        found = None
                        for name, cmdval in src_map.items():
                            if not isinstance(cmdval, str):
                                continue
                            try:
                                s = cmdval.strip()
                                if s.startswith("@"):
                                    s = s[1:]
                                if ":" in s:
                                    part = s.split(":", 1)[1]
                                else:
                                    mpart = re.search(r"([0-9A-Za-z]+)$", s)
                                    part = mpart.group(1) if mpart else s
                                cmd_code = re.sub(r"[^0-9A-Za-z]", "", part).upper()
                            except Exception:
                                cmd_code = str(cmdval).upper()

                            try:
                                dev = code
                                cmd = cmd_code
                                if cmd.lstrip("0") == dev.lstrip("0") or cmd == dev:
                                    found = name
                                    break
                                if len(dev) == 2 * len(cmd) and dev == cmd * 2:
                                    found = name
                                    break
                                if len(dev) == 2 and dev[0] == dev[1] and cmd == dev[0]:
                                    found = name
                                    break
                            except Exception:
                                if cmd_code == code:
                                    found = name
                                    break
                        if found:
                            self._source = found
                        else:
                            self._source = code
                            _LOGGER.debug("Unknown source code received (not persisting): %s", code)

            # Ensure UI shows a slider even if we haven't parsed a volume yet
            if self._volume_level is None:
                try:
                    feats = self.supported_features
                    if (MediaPlayerEntityFeature.VOLUME_SET in feats) or (MediaPlayerEntityFeature.VOLUME_STEP in feats):
                        self._volume_level = 0.5
                        _LOGGER.debug("Volume unknown — defaulting to level=%s to show slider", self._volume_level)
                except Exception:
                    pass

            self._available = True
                
        except Exception as exc:
            _LOGGER.exception("Error updating Marantz RS232 device: %s", exc)
            self._available = False
        finally:
            self._update_in_progress = False
