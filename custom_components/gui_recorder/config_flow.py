from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .db_stats import is_supported_recorder


class GuiRecorderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        database_check = is_supported_recorder(self.hass)
        if database_check is False:
            return self.async_abort(reason="unsupported_database")
        if database_check is None:
            return self.async_abort(reason="recorder_not_ready")

        return self.async_create_entry(title="GUI Recorder", data={})
