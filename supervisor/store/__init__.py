"""Add-on Store handler."""
import asyncio
import logging
from pathlib import Path
from typing import Dict, List

import voluptuous as vol

from supervisor.store.validate import SCHEMA_REPOSITORY_CONFIG
from supervisor.utils.json import read_json_file

from ..const import REPOSITORY_CORE, REPOSITORY_LOCAL
from ..coresys import CoreSys, CoreSysAttributes
from ..exceptions import JsonFileError
from .addon import AddonStore
from .data import StoreData
from .repository import Repository

_LOGGER: logging.Logger = logging.getLogger(__name__)

BUILTIN_REPOSITORIES = {REPOSITORY_CORE, REPOSITORY_LOCAL}


class StoreManager(CoreSysAttributes):
    """Manage add-ons inside Supervisor."""

    def __init__(self, coresys: CoreSys):
        """Initialize Docker base wrapper."""
        self.coresys: CoreSys = coresys
        self.data = StoreData(coresys)
        self.repositories: Dict[str, Repository] = {}

    @property
    def all(self) -> List[Repository]:
        """Return list of add-on repositories."""
        return list(self.repositories.values())

    async def load(self) -> None:
        """Start up add-on management."""
        self.data.update()

        # Init Supervisor built-in repositories
        repositories = set(self.sys_config.addons_repositories) | BUILTIN_REPOSITORIES

        # Init custom repositories and load add-ons
        await self.update_repositories(repositories)

    async def reload(self) -> None:
        """Update add-ons from repository and reload list."""
        tasks = [repository.update() for repository in self.repositories.values()]
        if tasks:
            await asyncio.wait(tasks)

        # read data from repositories
        self.data.update()
        self._read_addons()

    async def update_repositories(self, list_repositories):
        """Add a new custom repository."""
        new_rep = set(list_repositories)
        old_rep = set(self.repositories)

        # add new repository
        async def _add_repository(url):
            """Add a repository."""
            repository = Repository(self.coresys, url)
            if not await repository.load():
                _LOGGER.error("Can't load data from repository %s", url)
                return

            # don't add built-in repository to config
            if url not in BUILTIN_REPOSITORIES:
                # Verify that it is a add-on repository
                repository_file = Path(repository.git.path, "repository.json")
                try:
                    await self.sys_run_in_executor(
                        SCHEMA_REPOSITORY_CONFIG, read_json_file(repository_file)
                    )
                except (JsonFileError, vol.Invalid) as err:
                    _LOGGER.error("%s is not a valid add-on repository. %s", url, err)
                    await repository.remove()
                    return

                self.sys_config.add_addon_repository(url)

            self.repositories[url] = repository

        tasks = [_add_repository(url) for url in new_rep - old_rep]
        if tasks:
            await asyncio.wait(tasks)

        # del new repository
        for url in old_rep - new_rep - BUILTIN_REPOSITORIES:
            await self.repositories.pop(url).remove()
            self.sys_config.drop_addon_repository(url)

        # update data
        self.data.update()
        self._read_addons()

    def _read_addons(self) -> None:
        """Reload add-ons inside store."""
        all_addons = set(self.data.addons)

        # calc diff
        add_addons = all_addons - set(self.sys_addons.store)
        del_addons = set(self.sys_addons.store) - all_addons

        _LOGGER.info(
            "Loading add-ons from store: %d all - %d new - %d remove",
            len(all_addons),
            len(add_addons),
            len(del_addons),
        )

        # new addons
        for slug in add_addons:
            self.sys_addons.store[slug] = AddonStore(self.coresys, slug)

        # remove
        for slug in del_addons:
            self.sys_addons.store.pop(slug)
