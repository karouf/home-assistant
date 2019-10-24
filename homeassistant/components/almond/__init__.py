"""
Support for Almond.

For more details about this component, please refer to the documentation at
https://home-assistant.io/integrations/almond/
"""
import logging

from aiohttp import ClientSession
from pyalmond import AlmondLocalAuth, AbstractAlmondWebAuth, WebAlmondAPI
import voluptuous as vol

from homeassistant.const import CONF_TYPE, CONF_HOST
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.helpers import (
    config_validation as cv,
    config_entry_oauth2_flow,
    intent,
    aiohttp_client,
    storage,
)
from homeassistant import config_entries
from homeassistant.components import conversation

from . import config_flow
from .const import DOMAIN, TYPE_LOCAL, TYPE_OAUTH2

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN

DEFAULT_OAUTH2_HOST = "https://almond.stanford.edu"
DEFAULT_LOCAL_HOST = "http://localhost:3000"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Any(
            vol.Schema(
                {
                    vol.Required(CONF_TYPE): TYPE_OAUTH2,
                    vol.Required(CONF_CLIENT_ID): cv.string,
                    vol.Required(CONF_CLIENT_SECRET): cv.string,
                    vol.Optional(CONF_HOST, default=DEFAULT_OAUTH2_HOST): cv.url,
                }
            ),
            vol.Schema(
                {vol.Required(CONF_TYPE): TYPE_LOCAL, vol.Required(CONF_HOST): cv.url}
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    """Set up the Almond component."""
    hass.data[DOMAIN] = {}

    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]

    host = conf[CONF_HOST]

    if conf[CONF_TYPE] == TYPE_OAUTH2:
        config_flow.AlmondFlowHandler.async_register_implementation(
            hass,
            config_entry_oauth2_flow.LocalOAuth2Implementation(
                hass,
                DOMAIN,
                conf[CONF_CLIENT_ID],
                conf[CONF_CLIENT_SECRET],
                f"{host}/me/api/oauth2/authorize",
                f"{host}/me/api/oauth2/token",
            ),
        )
        return True

    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data={"type": TYPE_LOCAL, "host": conf[CONF_HOST]},
            )
        )
    return True


async def async_setup_entry(hass, entry):
    """Set up Almond config entry."""
    websession = aiohttp_client.async_get_clientsession(hass)
    if entry.data["type"] == TYPE_LOCAL:
        auth = AlmondLocalAuth(entry.data["host"], websession)

    else:
        # OAuth2
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        oauth_session = config_entry_oauth2_flow.OAuth2Session(
            hass, entry, implementation
        )
        auth = AlmondOAuth(entry.data["host"], websession, oauth_session)

    agent = AlmondAgent(WebAlmondAPI(auth))

    # Hass.io does its own configuration of Almond.
    if entry.data.get("is_hassio"):
        conversation.async_set_agent(hass, agent)
        return True

    # Configure Almond to connect to Home Assistant
    store = storage.Store(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load()

    if data is None:
        data = {}

    refresh_token = None
    if "almond_user" in data:
        user = await hass.auth.async_get_user(data["almond_user"])
        if user and user.refresh_tokens:
            refresh_token = list(user.refresh_tokens.values())[0]

    if refresh_token is None:
        user = await hass.auth.async_create_system_user("Almond", [GROUP_ID_ADMIN])
        refresh_token = await hass.auth.async_create_refresh_token(user)
        data["almond_user"] = user.id
        await store.async_save(data)

    resp = await auth.request(
        "post",
        "/api/devices/create",
        json={
            "kind": "io.home-assistant",
            "hassUrl": hass.config.api.base_url,
            "accessToken": "",
            "refreshToken": refresh_token.token,
            "accessTokenExpires": "0",
        },
    )
    if resp.status != 200:
        _LOGGER.warning(
            "Unable to configure Almond to work with Home Assistant: %s", resp.status
        )
        return False

    conversation.async_set_agent(hass, agent)
    return True


async def async_unload_entry(hass, entry):
    """Unload Almond."""
    conversation.async_set_agent(hass, None)
    return True


class AlmondOAuth(AbstractAlmondWebAuth):
    """Almond Authentication using OAuth2."""

    def __init__(
        self,
        host: str,
        websession: ClientSession,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ):
        """Initialize Almond auth."""
        super().__init__(host, websession)
        self._oauth_session = oauth_session

    async def async_get_access_token(self):
        """Return a valid access token."""
        if not self._oauth_session.is_valid:
            await self._oauth_session.async_ensure_token_valid()

        return self._oauth_session.token


class AlmondAgent(conversation.AbstractConversationAgent):
    """Almond conversation agent."""

    def __init__(self, api: WebAlmondAPI):
        """Initialize the agent."""
        self.api = api

    async def async_process(self, text: str) -> intent.IntentResponse:
        """Process a sentence."""
        response = await self.api.async_converse_text(text)

        buffer = ""
        for message in response["messages"]:
            if message["type"] == "text":
                buffer += "\n" + message["text"]
            elif message["type"] == "picture":
                buffer += "\n Picture: " + message["url"]
            elif message["type"] == "rdl":
                buffer += (
                    "\n Link: "
                    + message["rdl"]["displayTitle"]
                    + " "
                    + message["rdl"]["webCallback"]
                )
            elif message["type"] == "choice":
                buffer += "\n Choice: " + message["title"]

        intent_result = intent.IntentResponse()
        intent_result.async_set_speech(buffer.strip())
        return intent_result
