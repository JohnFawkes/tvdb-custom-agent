"""Register this agent's providers with a Plex Media Server.

Plex (tested on 1.43.5) does not show an "Episode ordering" library setting
for custom metadata agents: the library prefs list is fixed by Plex and it
rejects `showOrdering` on such libraries. Plex does, however, list every
metadata agent (provider group) in the library's Agent setting. So the
ordering a library uses by default is chosen by registering one agent per
ordering ("TheTVDB (self-hosted) - DVD order", ...) and picking it there.
Per-show orderings still come from Plex's own per-show "Episode ordering"
dropdown, which Plex fills from the SeasonType list this provider returns.

Each profile is registered as a provider (`POST /media/providers/metadata`)
plus an agent group with that provider as primary
(`POST /media/providers/metadata/group`). Re-running is safe: existing
providers get their URI updated and existing groups get renamed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from . import orders
from .config import Profile, Settings

log = logging.getLogger(__name__)

# Profiles registered when the caller does not choose: the root provider,
# DVD and absolute (the orderings people actually switch libraries to), and
# every profile defined in config.yaml.
DEFAULT_BUILTIN_PROFILES = ("dvd", "absolute")


class PlexError(Exception):
    pass


class PlexClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Accept": "application/json", "X-Plex-Product": "tvdb-custom-agent"}
        if token:
            headers["X-Plex-Token"] = token
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, params: dict | None = None) -> dict:
        try:
            resp = await self._http.request(method, path, params=params)
        except httpx.HTTPError as exc:
            raise PlexError(f"could not reach Plex at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise PlexError("Plex rejected the request (401): check the Plex token")
        if resp.status_code >= 400:
            text = " ".join(resp.text.split())[:300]
            raise PlexError(f"Plex returned {resp.status_code} for {method} {path}: {text}")
        if not resp.content or "json" not in resp.headers.get("content-type", ""):
            return {}
        return resp.json()

    async def providers(self) -> list[dict]:
        data = await self._call("GET", "/media/providers/metadata")
        return (data.get("MediaContainer") or {}).get("MetadataAgentProvider") or []

    async def add_provider(self, uri: str) -> dict:
        data = await self._call("POST", "/media/providers/metadata", {"uri": uri})
        found = (data.get("MediaContainer") or {}).get("MetadataAgentProvider") or []
        if not found:
            raise PlexError(f"Plex did not return the provider it registered for {uri}")
        return found[0]

    async def update_provider(self, provider_id: int, uri: str) -> None:
        await self._call("PUT", f"/media/providers/metadata/{int(provider_id)}", {"uri": uri})

    async def groups(self) -> list[dict]:
        data = await self._call("GET", "/media/providers/metadata/group")
        return (data.get("MediaContainer") or {}).get("MetadataAgentProviderGroup") or []

    async def create_group(self, title: str, primary_identifier: str) -> dict:
        data = await self._call(
            "POST", "/media/providers/metadata/group", {"title": title, "primaryIdentifier": primary_identifier}
        )
        found = (data.get("MediaContainer") or {}).get("MetadataAgentProviderGroup") or []
        if not found:
            raise PlexError(f"Plex did not return the agent it created for {primary_identifier}")
        return found[0]

    async def rename_group(self, group_id: int, title: str) -> None:
        await self._call("PUT", f"/media/providers/metadata/group/{int(group_id)}", {"title": title})


@dataclass
class Registration:
    profile: str  # "" for the root provider
    identifier: str
    uri: str
    agent: str  # agent (group) title shown in Plex
    providerId: int | None = None
    provider: str = ""  # created | updated | unchanged
    groupId: int | None = None
    group: str = ""  # created | renamed | unchanged
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def agent_title(settings: Settings, profile: Profile) -> str:
    """Name of the agent as it appears in Plex's library Agent setting."""
    if profile.name:
        return profile.title
    order = profile.episode_order
    label = "TVDB default" if order == orders.DEFAULT else orders.BY_SLUG[order].tag
    return f"{profile.title} - {label} order"


def provider_uri(agent_url: str, profile: Profile) -> str:
    return agent_url.rstrip("/") + profile.base_path


def default_profile_names(settings: Settings) -> list[str]:
    names = [""]
    names += [n for n in DEFAULT_BUILTIN_PROFILES if n in settings.profiles]
    names += [n for n, p in settings.profiles.items() if not p.builtin]
    return names


def resolve_profiles(settings: Settings, names: list[str] | None) -> list[Profile]:
    chosen = default_profile_names(settings) if names is None else names
    profiles: list[Profile] = []
    for name in chosen:
        key = (name or "").strip()
        prof = settings.root if key in ("", "/", "root", "default") else settings.profile(key)
        if prof is None:
            raise ValueError(f"unknown profile '{name}'")
        if prof not in profiles:
            profiles.append(prof)
    return profiles


async def register(
    plex: PlexClient, settings: Settings, profiles: list[Profile], agent_url: str
) -> list[Registration]:
    """Create or update one provider + agent group per profile in Plex."""
    if not agent_url or not agent_url.startswith(("http://", "https://")):
        raise ValueError("agent URL must be the http(s) address Plex uses to reach this agent")
    existing_providers = {p.get("identifier"): p for p in await plex.providers()}
    existing_groups = {g.get("primaryIdentifier"): g for g in await plex.groups()}
    results: list[Registration] = []
    for prof in profiles:
        reg = Registration(
            profile=prof.name or "",
            identifier=prof.identifier,
            uri=provider_uri(agent_url, prof),
            agent=agent_title(settings, prof),
        )
        try:
            current = existing_providers.get(prof.identifier)
            if current is None:
                created = await plex.add_provider(reg.uri)
                reg.providerId, reg.provider = created.get("id"), "created"
            else:
                reg.providerId = current.get("id")
                if (current.get("uri") or "").rstrip("/") != reg.uri.rstrip("/"):
                    await plex.update_provider(int(current["id"]), reg.uri)
                    reg.provider = "updated"
                else:
                    reg.provider = "unchanged"
            group = existing_groups.get(prof.identifier)
            if group is None:
                created_group = await plex.create_group(reg.agent, prof.identifier)
                reg.groupId, reg.group = created_group.get("id"), "created"
            else:
                reg.groupId = group.get("id")
                if group.get("title") != reg.agent:
                    await plex.rename_group(int(group["id"]), reg.agent)
                    reg.group = "renamed"
                else:
                    reg.group = "unchanged"
        except PlexError as exc:
            reg.error = str(exc)
            log.warning("Registering %s in Plex failed: %s", prof.identifier, exc)
        results.append(reg)
    return results
