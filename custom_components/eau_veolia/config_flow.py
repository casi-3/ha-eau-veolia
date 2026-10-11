from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import Account, VeoliaAuthError, VeoliaClient, VeoliaError
from .const import (
    CONF_SEWER_FIXED,
    CONF_SEWER_PRICE,
    CONF_WATER_FIXED,
    CONF_WATER_PRICE,
    DOMAIN,
    LOGGER,
)

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class EauVeoliaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> EauVeoliaOptionsFlow:
        return EauVeoliaOptionsFlow()

    async def _validate(self, email: str, password: str) -> tuple[Account | None, dict[str, str]]:
        client = VeoliaClient(async_get_clientsession(self.hass), email, password)
        try:
            return await client.async_get_account(), {}
        except VeoliaAuthError:
            return None, {"base": "invalid_auth"}
        except VeoliaError as err:
            LOGGER.warning("Validation Veolia impossible : %s", err)
            return None, {"base": "cannot_connect"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            account, errors = await self._validate(email, user_input[CONF_PASSWORD])
            if account:
                await self.async_set_unique_id(account.id_abonnement)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Compteur {account.numero_compteur or account.numero_pds}",
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            account, errors = await self._validate(
                entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if account:
                await self.async_set_unique_id(account.id_abonnement)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


PRICE_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0, max=100, step="any", mode=NumberSelectorMode.BOX)
)
FIXED_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=0, max=10000, step=0.01, mode=NumberSelectorMode.BOX)
)
OPTION_FIELDS = (
    (CONF_WATER_PRICE, PRICE_SELECTOR),
    (CONF_WATER_FIXED, FIXED_SELECTOR),
    (CONF_SEWER_PRICE, PRICE_SELECTOR),
    (CONF_SEWER_FIXED, FIXED_SELECTOR),
)


class EauVeoliaOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            options = {k: v for k, v in user_input.items() if v is not None}
            if options != dict(self.config_entry.options):
                self.hass.data.setdefault(DOMAIN, set()).add(self.config_entry.entry_id)
            return self.async_create_entry(data=options)
        options = self.config_entry.options
        schema = {
            vol.Optional(key, description={"suggested_value": options.get(key)}): selector
            for key, selector in OPTION_FIELDS
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
