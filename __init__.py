"""Marantz RS232 integration (minimal scaffold).

This file intentionally minimal. Platform setup happens in `media_player.py`.
"""

DOMAIN = "marantz_rs232"

async def async_setup(hass, config):
    """Set up the Marantz RS232 integration (YAML-style supported).

    Configuration is read by the platform implementation.
    """
    return True
