"""Config flow for Marantz RS232 integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import DOMAIN, DEFAULT_NAME, DEFAULT_BAUDRATE

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("serial_port", default="/dev/ttyUSB0"): str,
        vol.Optional("baudrate", default=DEFAULT_BAUDRATE): int,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
    }
)

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Marantz RS232."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}

        # Validate the input here if needed (e.g. check if port exists)
        # For now, we assume the user knows the path.

        # Create the entry
        return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
