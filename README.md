# tvdb-custom-agent

A self-hosted **Plex custom metadata provider** backed by **TheTVDB v4 API**.

Plex is dropping its built-in TVDB support. Plex Media Server 1.43.0 and later can
use custom metadata providers instead (see the
[Plex docs](https://developer.plex.tv/pms/index.html#section/API-Info/Metadata-Providers)).
This project is one of those providers. Run it next to Plex, point Plex at it, and
your TV libraries get show, season and episode metadata from TheTVDB again,
**with every TVDB episode ordering**: aired, DVD, absolute, alternate, regional,
alternate DVD and alternate 2. You can choose the ordering **per library and per show**.

## Features

- Full Plex provider API: provider root, `GET /library/metadata/{ratingKey}`,
  `/children`, `/grandchildren` (paged), `/images`, and `POST /library/metadata/matches`
  (automatic and manual "Fix Match").
- Shows: title, original title, summary, first-aired date, year, content rating
  (per `X-Plex-Country`), runtime, studio, networks, genres, country, cast and crew,
  posters, backgrounds, clear logos, theme music, and IMDb/TMDB/TVDB GUIDs.
- Seasons: titles (including named seasons), posters, summaries, air dates.
- Episodes: titles, summaries, air dates, runtime, stills, guest stars, directors,
  writers, and IMDb GUIDs.
- Translations: follows `X-Plex-Language` (for example `de-DE`), with fallback
  languages for anything TVDB hasn't translated.
- Episode orderings:
  - **Per library**: each ordering is its own Plex agent, so you pick the
    ordering in the library's **Agent** setting. **Register in Plex** (admin page
    or CLI) adds the agents for you. You can define your own profiles too.
  - **Per show**: Plex's own **Episode ordering** setting on each show lists the
    orderings TheTVDB has for it. You can also set overrides in the agent.
- Matching: by title and year, `{tvdb-12345}` / `{imdb-tt…}` / `{tmdb-…}` tags in
  folder names, TVDB/IMDb/TMDB GUIDs, season and episode numbers, or air date.
- A small admin web page (`/admin`) for searching shows, seeing which orderings
  each one has, and setting per-show overrides.
- In-memory caching with request coalescing, TVDB token renewal, and retries.

## Requirements

- Plex Media Server **1.43.0 or newer** (custom metadata providers).
- A **TheTVDB v4 API key** from <https://thetvdb.com/api-information>. If it is
  a "user-supported" key, you also need your subscriber **PIN**.
- Docker, or Python 3.11+.
- Plex must be able to reach the agent over HTTP. TVDB image URLs are public, so
  Plex clients load artwork straight from TheTVDB.

## Quick start (Docker)

Images for `linux/amd64` and `linux/arm64` are published to the GitHub Container
Registry as `ghcr.io/johnfawkes/tvdb-custom-agent`:

| Tag | Built from |
|---|---|
| `latest` | the `main` branch |
| `1.2.3`, `1.2` | release tags `v1.2.3` |
| `sha-<commit>` | every published commit |

Get the compose file into an empty folder:

```bash
mkdir tvdb-agent && cd tvdb-agent
```

```bash
curl -fsSLO https://raw.githubusercontent.com/JohnFawkes/tvdb-custom-agent/main/docker-compose.yml
```

Create a `.env` file next to `docker-compose.yml`:

```dotenv
TVDB_API_KEY=your-key
# TVDB_PIN=1234
ADMIN_TOKEN=pick-a-long-random-string
# TVDB_AGENT_TAG=1.2.3     # pin a release instead of latest
# TVDB_AGENT_PORT=8000     # host port
# PUID=1000                # user/group that owns ./data
# PGID=1000
```

```bash
mkdir -p data config
```

```bash
docker compose up -d
```

The container runs as UID/GID 1000 by default. Set `PUID` and `PGID` in `.env` if
`./data` belongs to a different user. The agent must be able to write there to
save per-show overrides. Put an optional `config.yaml` in `./config`.

Check that it runs: `http://<host>:8000/health` should return `{"status":"ok",…}`,
and `http://<host>:8000/` should return a `MediaProvider` document.

To update, run `docker compose pull` and then `docker compose up -d`.

Without compose:

```bash
docker run -d --name tvdb-agent --restart unless-stopped -p 8000:8000 -e TVDB_API_KEY=your-key -e ADMIN_TOKEN=change-me -v "$PWD/data:/data" ghcr.io/johnfawkes/tvdb-custom-agent:latest
```

### Building the image yourself

From a clone of this repository, uncomment `build: .` in `docker-compose.yml`
and run `docker compose up -d --build`, or:

```bash
docker build -t tvdb-custom-agent .
```

The [`docker` workflow](.github/workflows/docker.yml) builds the image on every
pull request and runs a smoke test with `docker-compose.yml`. It publishes to
GHCR from `main` and from `v*.*.*` tags.

### Without Docker

```bash
pip install .
```

```bash
TVDB_API_KEY=your-key tvdb-agent --port 8000
```

## Adding it to Plex

The easiest way is **Register in Plex** on the admin page
(`http://<agent-host>:8000/admin`), or the same thing from the command line:

```bash
docker exec tvdb-agent tvdb-agent register-plex --plex-url http://192.168.1.10:32400 --token YOUR_PLEX_TOKEN --agent-url http://192.168.1.10:8000
```

This adds one Plex **agent** per episode ordering. By default those are
"TheTVDB (self-hosted) - TVDB default order", "… - DVD order", "… - Absolute order",
plus every profile in your `config.yaml`. Use `--all` for every TVDB ordering, or
repeat `--profile NAME` to choose. Running it again is safe: it updates the URL
and name of agents that already exist.

- `--plex-url` is Plex as the **agent** reaches it.
- `--agent-url` is the agent as **Plex** reaches it. If Plex runs in Docker,
  `localhost` inside the Plex container is not the agent. Use the host's LAN IP,
  or put both containers on one Docker network and use `http://tvdb-agent:8000`.
- The token is the server owner's
  [X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
  Set `PLEX_URL`, `PLEX_TOKEN` and `PUBLIC_URL` to avoid passing them each time.

Then, in Plex, open a TV library's **Edit Library > Advanced > Agent**, pick the
ordering the library should use, save, and run **Refresh All Metadata**.

These steps were tested against Plex Media Server 1.43.5.

### Registering by hand (Plex API)

1. Register the provider. Use the root URL, or a profile URL such as
   `http://agent-host:8000/profiles/dvd`, URL-encoded in `uri`. Plex answers
   `409` if a provider with the same identifier already exists. In that case,
   change its URL with `PUT /media/providers/metadata/{id}?uri=…`.

   ```bash
   curl -X POST "http://PLEX:32400/media/providers/metadata?uri=http%3A%2F%2Fagent-host%3A8000&X-Plex-Token=TOKEN"
   ```

2. Create an agent (provider group) with that provider as the primary:

   ```bash
   curl -X POST "http://PLEX:32400/media/providers/metadata/group?title=TheTVDB&primaryIdentifier=tv.plex.agents.custom.tvdbcustomagent&X-Plex-Token=TOKEN"
   ```

3. Choose the agent in the library's settings, or call
   `PUT /library/sections/{id}?agent=<identifier>&metadataAgentProviderGroupId=<group id>`.
   Then run **Refresh All Metadata**.

## Episode ordering

TheTVDB keeps every alternative numbering of a show as a separate set of seasons.
This provider exposes all of them.

| Order | Profile URL | Plex `episodeOrder` id |
|---|---|---|
| Series default (usually aired) | `/` | — |
| Aired | `/profiles/aired` | `tvdbAiring` |
| DVD | `/profiles/dvd` | `tvdbDvd` |
| Absolute | `/profiles/absolute` | `tvdbAbsolute` |
| Alternate | `/profiles/alternate` | `tvdbAlternate` |
| Regional | `/profiles/regional` | `tvdbRegional` |
| Alternate DVD | `/profiles/altdvd` | `tvdbAltdvd` |
| Alternate 2 | `/profiles/alttwo` | `tvdbAlttwo` |

### Per library: pick the agent

Plex doesn't show a separate "Episode ordering" setting in the library settings
for custom agents. In Plex 1.43.5 the list of library settings is fixed by Plex,
and it refuses `showOrdering` on libraries that use a custom agent. So each
ordering is its own agent, and the library's **Agent** setting is where you
choose the ordering:

| Agent in Plex | Provider URL |
|---|---|
| TheTVDB (self-hosted) - TVDB default order | `http://agent:8000/` |
| TheTVDB (self-hosted) - DVD order | `http://agent:8000/profiles/dvd` |
| TheTVDB (self-hosted) - Absolute order | `http://agent:8000/profiles/absolute` |

After switching a library to another agent, run **Refresh All Metadata**. Plex
matches the shows again with the new agent and renumbers the episodes. Each
profile has its own identifier (`tv.plex.agents.custom.tvdbcustomagent.dvd`), so
the show GUIDs change with the agent.

To set a library-wide language, a custom agent name, or library-specific show
overrides, define your own profiles in `config.yaml`. **Register in Plex** adds
them as agents too.

```yaml
profiles:
  anime:
    title: "TheTVDB (Anime)"
    episode_order: absolute
    show_overrides:
      12345: aired     # this one show uses aired order in the anime library
```

If a show doesn't have the agent's ordering, the provider uses the show's
default ordering, so a DVD library still works for shows without a DVD order.
Set `fallback_to_default_order: false` to turn this off.

### Per show

1. **In Plex**, open the show's **Edit > Advanced > Episode ordering**. Plex
   fills this list from the orderings TheTVDB has for that show, such as
   "TheTVDB (Aired)", "TheTVDB (DVD)" and "TheTVDB (Absolute)". It also offers
   **Library default**, which means the ordering of the library's agent. Pick
   one, save, and run **Refresh Metadata** on the show. Plex then asks the
   agent for that ordering (`episodeOrder=tvdbDvd`).
2. **In the agent.** These overrides win over everything else, including the
   ordering chosen in Plex:
   - in `config.yaml`, under `show_overrides` (all agents) or under a profile's
     `show_overrides` (that agent only), keyed by TVDB series id;
   - or at runtime on the admin page `http://agent:8000/admin`, or through the API:

     ```bash
     curl -X PUT -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"order":"dvd"}' http://agent:8000/admin/api/overrides/global/72108
     ```

     Use a profile name instead of `global` to scope the override to one agent.
     Runtime overrides are saved in `DATA_DIR/overrides.json`.

After you change an ordering, run **Refresh Metadata** on the show in Plex. The
new numbering applies on the next refresh.

### Which ordering wins

1. Per-show override for this agent's profile (runtime, then `config.yaml`)
2. Global per-show override (runtime, then `config.yaml`)
3. The show's **Episode ordering** in Plex, sent as `episodeOrder`. It's ignored
   if the profile sets `honor_plex_episode_order: false`. "Library default" sends
   nothing.
4. The `episode_order` of the agent the library uses
5. The show's TVDB default ordering

The admin page shows, for each provider URL, which ordering a show will use and why.

## Configuration

Settings come from environment variables and/or a YAML file
(`/config/config.yaml`, `./config.yaml`, or the path in `CONFIG_FILE`).
Environment variables take precedence. See [`config.example.yaml`](config.example.yaml)
for every option.

| Variable | Default | Meaning |
|---|---|---|
| `TVDB_API_KEY` | *(required)* | TheTVDB v4 API key |
| `TVDB_PIN` | | Subscriber PIN for user-supported keys |
| `ADMIN_TOKEN` | | Protects `/admin` and `/admin/api/*`. If unset, the admin page is open |
| `PROVIDER_IDENTIFIER` | `tv.plex.agents.custom.tvdbcustomagent` | Provider identifier and GUID scheme. Choose it once: changing it changes every GUID |
| `PROVIDER_TITLE` | `TheTVDB (self-hosted)` | Name shown in Plex |
| `DEFAULT_LANGUAGE` | `en` | Language used when Plex doesn't send one |
| `DEFAULT_EPISODE_ORDER` | `default` | Ordering for the root provider |
| `HONOR_PLEX_EPISODE_ORDER` | `true` | Honor the ordering chosen in Plex |
| `BUILTIN_PROFILES` | `true` | Expose `/profiles/<ordering>` for every TVDB ordering |
| `THEME_MUSIC` | `true` | Add Plex's theme song URL for each show |
| `CACHE_TTL` | `43200` | Seconds to cache TVDB responses |
| `DATA_DIR` | `./data` (`/data` in Docker) | Where runtime overrides are stored |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `PUBLIC_URL` | | The agent's URL as Plex reaches it. Default for **Register in Plex**, and shown on the admin page |
| `PLEX_URL` | | Plex Media Server URL, for **Register in Plex** |
| `PLEX_TOKEN` | | Plex token, for **Register in Plex**. It can also be typed on the admin page instead |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs every match request Plex sends |

## How items are identified

| Item | ratingKey | GUID |
|---|---|---|
| Show | `series-<tvdbId>` | `<identifier>://show/series-81189` |
| Season | `season-<tvdbId>-<order>-<number>` | `<identifier>://season/season-81189-dvd-1` |
| Episode | `episode-<tvdbId>-<order>-<tvdbEpisodeId>` | `<identifier>://episode/episode-81189-dvd-349232` |

The ordering is part of each season and episode key. Once Plex has stored a
season, later `/children` calls return the same numbering whichever URL they
come through. Shows also carry `tvdb://`, `imdb://` and `tmdb://` GUIDs, so you
can combine this provider with others in a Plex agent group.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` or `/profiles/<name>` | `MediaProvider` definition |
| GET | `…/library/metadata/<ratingKey>` | Item metadata (`includeChildren=1` adds `Children`) |
| GET | `…/library/metadata/<ratingKey>/children` | Seasons of a show, or episodes of a season (paged) |
| GET | `…/library/metadata/<ratingKey>/grandchildren` | All episodes of a show (paged) |
| GET | `…/library/metadata/<ratingKey>/images` | All artwork for an item |
| GET | `…/library/metadata/<ratingKey>/extras` | Always empty (TVDB has no extras); Plex asks on every refresh |
| POST | `…/library/metadata/matches` | Match from hints (`type`, `title`, `year`, `guid`, `index`, `parentIndex`, `date`, `filename`, `manual`, …) |
| GET | `/profiles` | All provider URLs and identifiers |
| GET | `/health` | Health check |
| GET | `/admin` | Admin page |
| POST | `/admin/api/plex/register` | Register in Plex: `{"plexUrl", "token", "agentUrl", "profiles": ["", "dvd"]}` |

Paging uses `X-Plex-Container-Start` and `X-Plex-Container-Size`, sent as headers
or query parameters. Without them, `/children` and `/grandchildren` return the
first 20 items, as the Plex spec requires. `includeFields`, `excludeFields`,
`includeElements` and `excludeElements` are supported.

## Troubleshooting

- **Plex says the provider is offline.** From the Plex host or container, check
  that `curl http://agent:8000/` returns JSON.
- **All requests return 500 "TVDB authentication failed".** Check `TVDB_API_KEY`
  and, for user-supported keys, `TVDB_PIN`.
- **Wrong match.** Use **Fix Match** in Plex (it sends `manual=1` and shows
  ranked results), or add `{tvdb-<id>}` to the show's folder name, for example
  `Galaxy Patrol (2001) {tvdb-12345}`.
- **Episodes don't line up.** Your files probably follow a different ordering.
  Use the admin page to see which orderings the show has and how many episodes
  each contains, then set an override or move the show to a library with
  the matching profile.
- **An agent's changes don't show up.** Plex keeps each agent's definition in
  memory until Plex restarts. New agents appear right away.
- **Seeing what Plex sends.** Set `LOG_LEVEL=DEBUG`. Every request is logged,
  including match bodies.

## Development

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest -q
```

The tests run the full HTTP stack against an in-process fake of the TVDB API
(`tests/fake_tvdb.py`), so they need no API key or network.

Layout:

```
src/tvdb_agent/
  app.py        FastAPI routes, paging, profile prefixes, admin API
  service.py    order resolution, show/season/episode builders, matching
  mapper.py     TVDB record -> Plex field helpers (artwork, people, ratings, GUIDs)
  tvdb.py       async TVDB v4 client (auth, retries, pagination, caching)
  orders.py     TVDB season types <-> Plex SeasonType / episodeOrder
  config.py     env + YAML settings and profiles
  overrides.py  per-show order overrides (config + runtime JSON store)
  plex.py       "Register in Plex": one Plex agent per ordering
  keys.py       ratingKey / GUID helpers, {tvdb-…} folder tag parsing
  matching.py   title normalisation and scoring
  i18n.py       Plex language/country -> TVDB codes
```

## Notes

This project is not affiliated with Plex or TheTVDB. Follow TheTVDB's API terms
for your key. Metadata and artwork belong to TheTVDB and its contributors.
