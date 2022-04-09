"""Component to create and automatically remove Home Assistant backups."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from os import getenv
from os.path import join, isfile
from typing import List, Dict, Tuple

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.backup.const import DOMAIN as DOMAIN_BACKUP
from homeassistant.components.hassio import (
    ATTR_FOLDERS,
    ATTR_ADDONS,
    ATTR_PASSWORD,
    is_hassio,
)
from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType, HomeAssistantType, ServiceCallType
from homeassistant.util import dt as dt_util
from slugify import slugify

from .const import (
    DOMAIN,
    EVENT_BACKUP_FAILED,
    EVENT_BACKUPS_PURGED,
    EVENT_BACKUP_SUCCESSFUL,
    EVENT_BACKUP_START,
    UNSUB_LISTENER,
    DATA_AUTO_BACKUP,
    DEFAULT_BACKUP_TIMEOUT_SECONDS,
    CONF_AUTO_PURGE,
    CONF_BACKUP_TIMEOUT,
    DEFAULT_BACKUP_TIMEOUT,
)
from .handlers import SupervisorHandler, HassioAPIError, BackupHandler, HandlerBase

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "snapshots_expiry"
STORAGE_VERSION = 1

ATTR_KEEP_DAYS = "keep_days"
ATTR_INCLUDE = "include"
ATTR_EXCLUDE = "exclude"
ATTR_DOWNLOAD_PATH = "download_path"

DEFAULT_BACKUP_FOLDERS = {
    "ssl": "ssl",
    "share": "share",
    "media": "media",
    "addons": "addons/local",
    "config": "homeassistant",
    "local add-ons": "addons/local",
    "home assistant configuration": "homeassistant",
}

SERVICE_PURGE = "purge"
SERVICE_BACKUP_FULL = "backup_full"
SERVICE_BACKUP_PARTIAL = "backup_partial"

SCHEMA_BACKUP_BASE = vol.Schema(
    {
        vol.Optional(ATTR_NAME): cv.string,
        vol.Optional(ATTR_PASSWORD): cv.string,
        vol.Optional(ATTR_KEEP_DAYS): vol.Coerce(float),
        vol.Optional(ATTR_DOWNLOAD_PATH): cv.isdir,
    }
)

SCHEMA_ADDONS_FOLDERS = {
    vol.Optional(ATTR_FOLDERS, default=[]): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ADDONS, default=[]): vol.All(cv.ensure_list, [cv.string]),
}

SCHEMA_BACKUP_FULL = SCHEMA_BACKUP_BASE.extend(
    {vol.Optional(ATTR_EXCLUDE): SCHEMA_ADDONS_FOLDERS}
)

SCHEMA_BACKUP_PARTIAL = SCHEMA_BACKUP_BASE.extend(SCHEMA_ADDONS_FOLDERS)

MAP_SERVICES = {
    SERVICE_BACKUP_FULL: SCHEMA_BACKUP_FULL,
    SERVICE_BACKUP_PARTIAL: SCHEMA_BACKUP_PARTIAL,
    SERVICE_PURGE: None,
}

PLATFORMS = ["sensor"]


@callback
def is_backup(hass: HomeAssistant) -> bool:
    return DOMAIN_BACKUP in hass.config.components


async def async_setup(hass: HomeAssistantType, config: ConfigType):
    """Set up the Auto Backup component."""
    hass.data.setdefault(DOMAIN, {})
    if DOMAIN in config:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data=config[DOMAIN],
            )
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Auto Backup from a config entry."""
    _LOGGER.info("Setting up Auto Backup config entry %s", entry.entry_id)

    # check backup integration or supervisor is available
    if not is_hassio(hass) and not is_backup(hass):
        _LOGGER.error(
            "You must be running Home Assistant Supervised or have the 'backup' integration enabled."
        )
        return False

    options = entry.data or entry.options
    options = {
        CONF_AUTO_PURGE: options.get(CONF_AUTO_PURGE, True),
        CONF_BACKUP_TIMEOUT: options.get(CONF_BACKUP_TIMEOUT, DEFAULT_BACKUP_TIMEOUT),
    }

    if is_hassio(hass):
        handler = SupervisorHandler(getenv("SUPERVISOR"), async_get_clientsession(hass))
    else:
        handler = BackupHandler(hass.data[DOMAIN_BACKUP])

    auto_backup = AutoBackup(hass, options, handler)
    hass.data[DOMAIN][DATA_AUTO_BACKUP] = auto_backup
    hass.data[DOMAIN][UNSUB_LISTENER] = entry.add_update_listener(
        auto_backup.update_listener
    )

    await auto_backup.load_snapshots_expiry()

    ### REGISTER SERVICES ###
    async def async_service_handler(call: ServiceCallType):
        """Handle Auto Backup service calls."""
        if call.service == SERVICE_PURGE:
            await auto_backup.purge_backups()
        else:
            data = call.data.copy()
            if call.service == SERVICE_BACKUP_PARTIAL:
                data[ATTR_INCLUDE] = {
                    ATTR_FOLDERS: data.pop(ATTR_FOLDERS, []),
                    ATTR_ADDONS: data.pop(ATTR_ADDONS, []),
                }
            await auto_backup.async_create_backup(data)

    for service, schema in MAP_SERVICES.items():
        hass.services.async_register(DOMAIN, service, async_service_handler, schema)

    # load the auto backup sensor.
    for component in PLATFORMS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )

    hass.data[DOMAIN][UNSUB_LISTENER]()

    for service in MAP_SERVICES.keys():
        hass.services.async_remove(DOMAIN, service)

    return unload_ok


class AutoBackup:
    def __init__(self, hass: HomeAssistantType, options: Dict, handler: HandlerBase):
        self._hass = hass
        self._handler = handler
        self._auto_purge = options[CONF_AUTO_PURGE]
        self._backup_timeout = options[CONF_BACKUP_TIMEOUT] * 60
        self._state = 0
        self._snapshots = {}
        self._supervised = is_hassio(hass)
        self._store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{STORAGE_KEY}", encoder=JSONEncoder
        )

    async def update_listener(self, hass, entry: ConfigEntry):
        """Handle options update."""
        self._auto_purge = entry.options[CONF_AUTO_PURGE]
        self._backup_timeout = entry.options[CONF_BACKUP_TIMEOUT] * 60

    async def load_snapshots_expiry(self):
        """Load snapshots expiry dates from Home Assistant's storage."""
        data = await self._store.async_load()

        if data is not None:
            for slug, expiry in data.items():
                self._snapshots[slug] = datetime.fromisoformat(expiry)

    @property
    def monitored(self):
        return len(self._snapshots)

    @property
    def purgeable(self):
        return len(self.get_purgeable_snapshots())

    @property
    def state(self):
        return self._state

    @classmethod
    def ensure_slugs(cls, inclusion, installed_addons) -> Tuple[List, List]:
        """Helper method to slugify both the addon and folder sections"""
        addons = inclusion[ATTR_ADDONS]
        folders = inclusion[ATTR_FOLDERS]
        return (
            cls.ensure_addon_slugs(addons, installed_addons),
            cls.ensure_folder_slugs(folders),
        )

    @staticmethod
    def ensure_addon_slugs(addons, installed_addons) -> List[str]:
        """Replace addon names with their appropriate slugs."""
        if not addons:
            return []

        def match_addon(addon):
            for installed_addon in installed_addons:
                # perform case-insensitive match.
                if addon.casefold() == installed_addon["name"].casefold():
                    return installed_addon["slug"]
                if addon == installed_addon["slug"]:
                    return addon
            _LOGGER.warning("Addon '%s' does not exist", addon)

        return [match_addon(addon) for addon in addons]

    @staticmethod
    def ensure_folder_slugs(folders) -> List[str]:
        """Convert folder name to lower case and replace friendly folder names."""
        if not folders:
            return []

        def match_folder(folder):
            folder = folder.casefold()
            return DEFAULT_BACKUP_FOLDERS.get(folder, folder)

        return [match_folder(folder) for folder in folders]

    def generate_backup_name(self) -> str:
        if not self._supervised:
            return "Unknown"
        time_zone = dt_util.get_time_zone(self._hass.config.time_zone)
        return datetime.now(time_zone).strftime("%A, %b %d, %Y")

    async def async_create_backup(self, data: Dict):
        """Identify actual type of backup to create and handle include/exclude options"""
        if not self._supervised:
            disallowed_options = [ATTR_NAME, ATTR_PASSWORD]
            for option in disallowed_options:
                if option in data:
                    raise HomeAssistantError(
                        f"The '{option}' option is not supported on non-supervised installations."
                    )

            if ATTR_INCLUDE in data or ATTR_EXCLUDE in data:
                raise HomeAssistantError(
                    f"Partial backups (e.g. include/exclude) are not supported on non-supervised installations."
                )

        if ATTR_NAME not in data:
            data[ATTR_NAME] = self.generate_backup_name()

        _LOGGER.debug("Creating backup '%s'", data[ATTR_NAME])

        include: Dict = data.pop(ATTR_INCLUDE, None)
        exclude: Dict = data.pop(ATTR_EXCLUDE, None)

        if not (include or exclude):
            # must be a full backup
            await self._async_create_backup(data)
        else:
            installed_addons = await self._handler.get_installed_addons()

            addons = installed_addons
            folders = set(DEFAULT_BACKUP_FOLDERS.values())

            if include:
                addons, folders = self.ensure_slugs(include, installed_addons)

            if exclude:
                excluded_addons, excluded_folders = self.ensure_slugs(
                    exclude, installed_addons
                )
                addons = [
                    addon["slug"]
                    for addon in addons
                    if addon["slug"] not in excluded_addons
                ]
                folders = [
                    folder for folder in folders if folder not in excluded_folders
                ]

            data[ATTR_ADDONS] = addons
            data[ATTR_FOLDERS] = folders
            await self._async_create_backup(data, partial=True)

        ### PURGE BACKUPS ###
        if self._auto_purge:
            await self.purge_backups()

    async def _async_create_backup(self, data: Dict, partial: bool = False):
        """Create backup, update state, fire events, download backup and purge old backups"""
        keep_days = data.pop(ATTR_KEEP_DAYS, None)
        download_path = data.pop(ATTR_DOWNLOAD_PATH, None)

        ### LOG DEBUG INFO ###
        # ensure password is scrubbed from logs
        password = data.get(ATTR_PASSWORD)
        if password:
            data[ATTR_PASSWORD] = "<hidden>"

        _LOGGER.debug(
            "Creating backup (%s); keep_days: %s, timeout: %s, data: %s",
            "partial" if partial else "full",
            keep_days,
            self._backup_timeout,
            data,
        )

        # re-add password if it existed
        if password:
            data[ATTR_PASSWORD] = password
            del password

        ### CREATE BACKUP ###
        self._state += 1
        self._hass.bus.async_fire(EVENT_BACKUP_START, {"name": data[ATTR_NAME]})

        try:
            try:
                result = await self._handler.create_backup(
                    data, partial, timeout=self._backup_timeout
                )
            except HassioAPIError as err:
                raise HassioAPIError(
                    str(err) + ". There may be a backup already in progress."
                )

            # backup creation was successful
            slug = result["slug"]
            name = result.get(ATTR_NAME, data[ATTR_NAME])

            _LOGGER.info("Backup created successfully: '%s' (%s)", name, slug)

            self._state -= 1
            self._hass.bus.async_fire(
                EVENT_BACKUP_SUCCESSFUL, {"name": name, "slug": slug}
            )

            if keep_days is not None:
                # set snapshot expiry
                self._snapshots[slug] = datetime.now(timezone.utc) + timedelta(
                    days=float(keep_days)
                )
                # write snapshot expiry to storage
                await self._store.async_save(self._snapshots)

            # download backup to location if specified
            if download_path:
                self._hass.async_create_task(
                    self.async_download_backup(name, slug, download_path)
                )

        except Exception as err:
            _LOGGER.error("Error during backup. %s", err)
            self._state -= 1
            self._hass.bus.async_fire(
                EVENT_BACKUP_FAILED,
                {"name": data[ATTR_NAME], "error": str(err)},
            )

    def get_purgeable_snapshots(self) -> List[str]:
        """Returns the slugs of purgeable snapshots."""
        now = datetime.now(timezone.utc)
        return [slug for slug, expires in self._snapshots.items() if expires < now]

    async def purge_backups(self):
        """Purge expired backups from the Supervisor."""
        purged = [
            slug
            for slug in self.get_purgeable_snapshots()
            if await self._purge_snapshot(slug)
        ]

        if purged:
            _LOGGER.info(
                "Purged %s backups: %s",
                len(purged),
                purged,
            )
            self._hass.bus.async_fire(EVENT_BACKUPS_PURGED, {"backups": purged})
            # write updated snapshots list to storage
            await self._store.async_save(self._snapshots)
        else:
            _LOGGER.debug("No backups required purging.")

    async def _purge_snapshot(self, slug):
        """Purge an individual snapshot from Hass.io."""
        _LOGGER.debug("Attempting to remove backup: %s", slug)
        try:
            await self._handler.remove_backup(slug)
            # remove snapshot expiry.
            del self._snapshots[slug]
        except HassioAPIError as err:
            if str(err) == "Backup does not exist":
                del self._snapshots[slug]
            else:
                _LOGGER.error("Failed to purge backup: %s", err)
                return False
        return True

    def async_download_backup(self, name, slug, backup_path):
        """Download backup to the specified location."""

        # ensure the name is a valid filename.
        if name:
            filename = slugify(name, lowercase=False, separator="_")
        else:
            filename = slug

        # ensure the filename is a tar file.
        if not filename.endswith(".tar"):
            filename += ".tar"

        destination = join(backup_path, filename)

        # check if file already exists
        if isfile(destination):
            destination = join(backup_path, f"{slug}.tar")

        return self._handler.download_backup(
            slug, destination, timeout=self._backup_timeout
        )
