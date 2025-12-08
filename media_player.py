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
    """

    def __init__(self, port: str, baudrate: int, newline: str = "\r"):
        self.port = port
        self.baudrate = baudrate
        self.newline = newline
        self._serial: Optional[serial.Serial] = None

    def open(self):
        if self._serial is None or not self._serial.is_open:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=1)

    def close(self):
        if self._serial and self._serial.is_open:
            self._serial.close()

    def write_raw(self, raw: str) -> None:
        self.open()
        # Ensure start character '@' is present per spec
        if not raw.startswith("@"):
            raw = "@" + raw
        payload = raw.encode("ascii", errors="ignore") + self.newline.encode()
        # Debug: log exact command and payload sent to the serial port
        _LOGGER.debug("SerialDevice.write_raw: port=%s command=%s payload=%s", self.port, raw, payload)
        self._serial.write(payload)
        # small delay to let the device process
        time.sleep(0.08)

    def query(self, raw: str) -> str:
        """Send a command and return the raw response as text."""
        self.open()
        self._serial.reset_input_buffer()
        payload = raw.encode("ascii", errors="ignore") + self.newline.encode()
        self._serial.write(payload)
        # Give device a bit more time to respond
        time.sleep(0.12)

        # Read until terminator (CR) or until timeout; aggregate multiple lines
        end_time = time.time() + 0.5
        buf = bytearray()
        try:
            while time.time() < end_time:
                # Read until CR (self.newline) or timeout
                chunk = self._serial.read_until(self.newline.encode(), size=1024)
                if chunk:
                    buf.extend(chunk)
                    # small pause to allow additional bytes to arrive
                    time.sleep(0.02)
                    # continue loop to gather any further fragments
                    continue
                # no chunk available, break early
                break
        except serial.SerialException:
            return ""

        if not buf:
            return ""

        try:
            text = buf.decode("ascii", errors="ignore")
        except Exception:
            return ""

        # Normalize line endings and strip whitespace
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        _LOGGER.debug("SerialDevice.query: port=%s cmd=%s raw_response=%s", self.port, raw, repr(text))
        return text


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Marantz RS232 media player from YAML configuration."""
    name = config.get(CONF_NAME)
    port = config["serial_port"]
    baud = config.get("baudrate")
    command_map = config.get("command_map", {}) or {}

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

    optimistic = config.get("optimistic", False)
    use_marantzusb_format = config.get("use_marantzusb_format", False)

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
    poll_interval = config.get("poll_interval", 10)
    try:
        if poll_interval and int(poll_interval) > 0:
            interval = timedelta(seconds=int(poll_interval))

            async def _async_poll(now):
                try:
                    await entity.async_update()
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
    """Media player entity for a Marantz SR6001 (RS232).

    This entity envoie les commandes définies dans `command_map`. Le mapping est
    volontairement fourni par l'utilisateur car les chaînes RS232 exactes dépendent du firmware/format.
    Voir le README pour des exemples et comment remplir la map.
    """

    @property
    def extra_state_attributes(self):
        """Expose les attributs supplémentaires pour Home Assistant."""
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
        # Lock to serialize write operations to the serial port
        # Single I/O lock to serialize all serial reads/writes (prevents
        # overlapping queries that mix responses).
        self._io_lock = asyncio.Lock()
        # None = unknown, True/False = whether absolute volume set is supported
        self._absolute_supported = None
        # Debounce task and pending volume for slider movements
        self._debounce_task = None
        self._pending_volume = None
        # debounce interval in seconds for slider aggregation
        self._debounce_interval = 0.35

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
        """Retourne la valeur du volume en dB exacte renvoyée par l'ampli (si disponible)."""
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

    async def _write_opportunistic(self, cmd: str, verify_cmd: Optional[str] = None) -> None:
        """Try to write quickly even if the I/O lock is held.

        Attempt to acquire `_io_lock` with a short timeout. If acquired,
        perform the write under the lock (safe). If the lock cannot be
        acquired quickly, perform the write anyway (opportunistic) and
        schedule a short delayed verification under `_io_lock` using
        `verify_cmd` when provided.
        """
        lock_acquired = False
        try:
            try:
                await asyncio.wait_for(self._io_lock.acquire(), timeout=0.12)
                lock_acquired = True
            except asyncio.TimeoutError:
                lock_acquired = False

            # Perform the actual write regardless; if we have the lock we
            # keep it while writing, otherwise we write opportunistically.
            await self.hass.async_add_executor_job(self._device.write_raw, cmd)
        finally:
            if lock_acquired:
                try:
                    self._io_lock.release()
                except Exception:
                    pass

        # If we wrote without owning the lock and a verification command is
        # provided, schedule a short delayed verification under the lock so
        # that eventual consistency is achieved.
        if (not lock_acquired) and verify_cmd:
            async def _verify():
                await asyncio.sleep(0.18)
                async with self._io_lock:
                    try:
                        resp = await self.hass.async_add_executor_job(self._device.query, verify_cmd)
                    except Exception:
                        resp = None
                    _LOGGER.debug("opportunistic verify %s -> %s", verify_cmd, resp)

            asyncio.create_task(_verify())

    async def _apply_volume(self, volume: float) -> None:
        """Apply a volume change (called after debounce)."""
        # This reuses the previous logic for applying volume: prefer absolute
        # set if supported, otherwise use stepping. It's run under debounce
        # so frequent slider moves won't flood the serial port.
        if volume is None:
            return
        volume = max(0.0, min(1.0, float(volume)))

        # Range used by many Marantz devices
        max_db = 18.0
        min_db = -80.0

        tmpl = self._cmd.get("volume_set_template") or self._cmd.get("volume_set")

        # Attempt absolute set if available and not previously marked unsupported
        if tmpl and (self._absolute_supported is not False):
            db = min_db + volume * (max_db - min_db)
            # Build two candidate absolute commands:
            # 1) the existing template (e.g. "@VOL:{value:03d}") if provided
            # 2) Marantz-usb style absolute command (e.g. "@VOL:0-23") which many users report works faster
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

            # marantzusb-style: prefix '0' then signed integer decibels, e.g. '@VOL:0-23'
            marantzusb_cmd = None
            try:
                db_int = int(round(db))
                marantzusb_cmd = f"@VOL:0{db_int:+d}"
            except Exception:
                marantzusb_cmd = None

            # Build candidate list depending on config preference:
            # - if user requests marantzusb format, try that only
            # - otherwise try template first (if present), then marantzusb as fallback
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

            # Try candidates in order until verification succeeds
            query_cmd = self._cmd.get("query_volume")
            new_level = None
            for cmd in candidate_cmds:
                await self._write_opportunistic(cmd, verify_cmd=query_cmd)

                # verify
                if query_cmd:
                    await asyncio.sleep(0.12)
                    async with self._io_lock:
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
                    break

            if new_level is None:
                # none of the absolute candidates worked
                self._absolute_supported = False
                _LOGGER.debug("Debounced: absolute set did not take effect (new_level=%s), falling back to step commands", new_level)

        # Fallback: compute steps in dB and send that many +/- commands
        up_cmd = self._cmd.get("volume_up")
        down_cmd = self._cmd.get("volume_down")
        if not up_cmd and not down_cmd:
            _LOGGER.warning("No volume step commands available; cannot apply volume change")
            return

        # Query current volume if possible to be accurate
        current_level = self._volume_level
        query_cmd = self._cmd.get("query_volume")
        if query_cmd:
            try:
                async with self._io_lock:
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
            async with self._io_lock:
                await self.hass.async_add_executor_job(self._device.write_raw, up_cmd if volume > 0.5 else down_cmd)
            return

        current_db = min_db + current_level * (max_db - min_db)
        target_db = min_db + volume * (max_db - min_db)
        delta_db = target_db - current_db
        steps = int(round(abs(delta_db)))
        if steps <= 0:
            return

        cmd = up_cmd if delta_db > 0 else down_cmd
        async with self._io_lock:
            for _ in range(steps):
                await self.hass.async_add_executor_job(self._device.write_raw, cmd)
                await asyncio.sleep(0.08)

        # After applying steps, query once and update internal state
        if query_cmd:
            try:
                async with self._io_lock:
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
                        if re.fullmatch(r"\\d{3}", sval):
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

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume to a specific level (0..1).

        This method debounces rapid slider updates and schedules the actual
        application in `_apply_volume` to avoid flooding the serial port.
        """
        if volume is None:
            return
        volume = max(0.0, min(1.0, float(volume)))

        # optimistic immediate UI update
        if self._optimistic:
            self._volume_level = volume
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
        """Poll the device for current power, volume and source state."""
        try:
            # Serialize the entire update sequence to avoid overlapping queries/writes
            # which produce interleaved/mixed responses from the device.
            async with self._io_lock:
                # QUERY POWER
                if "query_power" in self._cmd:
                    try:
                        resp = await self.hass.async_add_executor_job(self._device.query, self._cmd.get("query_power"))
                    except Exception:
                        resp = None
                    _LOGGER.debug("query_power response: %s", resp)
                    if resp:
                        m = re.search(r"PWR[: ]\s*([0-9A-Za-z]+)", resp, re.IGNORECASE)
                        if m:
                            code = m.group(1).strip()
                            # device sent numeric power state; common: 1=on,2=off
                            try:
                                if code.isdigit():
                                    v = int(code)
                                    # In our command_map 2==ON, 1==OFF for this Marantz model
                                    self._is_on = bool(v == 2)
                                else:
                                    self._is_on = code.lower() in ("on", "1", "true")
                            except Exception:
                                self._is_on = None

                # QUERY VOLUME
                if "query_volume" in self._cmd:
                    try:
                        resp = await self.hass.async_add_executor_job(self._device.query, self._cmd.get("query_volume"))
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
                                # Stocke la valeur brute exacte
                                self._volume_db_exact = db
                                # Conversion pour le slider (adapter la plage si besoin)
                                self._volume_level = (db - (-72.0)) / (15.0 - (-72.0))
                                self._volume_level = max(0.0, min(1.0, self._volume_level))
                                _LOGGER.debug("Parsed volume dB=%s -> level=%s", db, self._volume_level)
                            except Exception:
                                self._volume_db_exact = None

                # QUERY SOURCE
                if "query_source" in self._cmd:
                    try:
                        resp = await self.hass.async_add_executor_job(self._device.query, self._cmd.get("query_source"))
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

                                # Normalize the command value to extract the device code.
                                # Accept formats like '@SRC:22', 'SRC:22', '22', 'SRC 22', etc.
                                try:
                                    s = cmdval.strip()
                                    if s.startswith("@"):
                                        s = s[1:]
                                    # Prefer the part after ':' if present, otherwise
                                    # try to take a trailing alnum group.
                                    if ":" in s:
                                        part = s.split(":", 1)[1]
                                    else:
                                        mpart = re.search(r"([0-9A-Za-z]+)$", s)
                                        part = mpart.group(1) if mpart else s
                                    cmd_code = re.sub(r"[^0-9A-Za-z]", "", part).upper()
                                except Exception:
                                    cmd_code = str(cmdval).upper()

                                # Compare normalized codes, allowing for leading zeros
                                try:
                                    dev = code
                                    cmd = cmd_code
                                    if cmd.lstrip("0") == dev.lstrip("0") or cmd == dev:
                                        found = name
                                        break

                                    # Handle devices that appear to repeat the code
                                    # characters (e.g. returns '11' for configured
                                    # '1'). Match when the device string equals the
                                    # command code duplicated, or when it's a double
                                    # character equal to the single command char.
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
                                # Unknown source code returned by device. Do NOT
                                # persist automatic mappings to `command_map_parsed.yaml`
                                # (this was adding noisy SRC_xx entries whenever the
                                # source changed). Instead, set the current source to
                                # the raw code so the UI reflects the device state
                                # without modifying the persistent mapping.
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
        except Exception as exc:  # pragma: no cover - runtime errors handled here
            _LOGGER.exception("Error updating Marantz RS232 device: %s", exc)
            self._available = False
