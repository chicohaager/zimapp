# zimapp — compose URL → ZimaOS app

Takes a **URL to a `docker-compose.yml`** (or a `Dockerfile`, or a
Docker image), builds a ZimaOS-conformant compose with an `x-casaos` block from
it, validates it against the known pitfalls and installs it via the official
ZimaOS API. **Multi-service stacks (app + database + cache) become exactly
one ZimaOS app.**

There is a **web UI** (color scheme from the ZFW dashboard) and a CLI.

![The web UI after analyzing the paperless-ngx compose: the source it followed,
the three detected services with the WebUI service marked `main`, and the app
metadata that becomes the `x-casaos` block](docs/screenshot-convert.jpg)

The generated compose is editable and re-validated before it is installed —
and it can be saved as a blueprint, downloaded or handed straight to ZimaOS:

![The result step: the generated ZimaOS compose, the notes that came out of the
conversion, and the install form](docs/screenshot-result.jpg)

Verified on **ZimaOS v1.7.0-beta1** (host 192.168.1.100), as of 19.07.2026:
paperless-ngx (webserver + postgres + redis) generated from the upstream URL,
installed, container `healthy`, tile in the grid, login as superuser proven.
zimapp itself runs there as an app on port 8790 — since 21.07.2026 from the
released image, applied to the running installation with `zimapp.py update`
(see the header of `zimapp-zimaos.yml` for what was measured).

> **A note on the `§` references.** They point into a private ZimaOS knowledge
> base that is not part of this repository — measurements of undocumented ZimaOS
> behaviour collected while building this. They are kept in the text because they
> say *where a rule comes from*, not because you can look them up. Every rule they
> back is spelled out in full here as well.

## Why

ZimaOS expects a compose with an `x-casaos` block. Its rules are not
documented, and the system acknowledges violations either with a cryptic
message or — worse — with nothing at all: the app runs, but never shows up in
the grid. On top of that, a compose from the net builds on things that do not
exist on ZimaOS: an `.env` next to it, relative paths like `./data`, named
volumes, arbitrary host ports. `zimapp` translates that once, instead of
guessing it anew per app.

## Web UI

```bash
pip install pyyaml          # only dependency
python3 zimapp.py serve     # http://127.0.0.1:8790
```

Flow: throw in a URL → look at the detected stack (who is WebUI, who is
database) → fill in metadata and variables → generate compose → validate →
install. Generated passwords are displayed in cleartext, because otherwise they
would only be in the compose.

Instead of a URL, **Open .yml…** reads a file from the machine the browser runs
on. Typing a path into the URL field does *not* do that — the server would look
for it inside its own container. A file and a URL never apply at the same time:
opening one clears the other, with a note saying which source is in use.

The UI binds to `127.0.0.1` on purpose: it accepts ZimaOS credentials and loads
arbitrary URLs. `--bind 0.0.0.0` works, but has to be set deliberately (and
then needs an open ZFW port, the ZimaOS firewall (ZFW) docs).

⚠️ **There is no login yet, so do not publish the port to your network.**
Running it as a ZimaOS app does exactly that: the container binds to `0.0.0.0`
and the generated compose publishes the port, which makes the whole API reachable
for anyone on the LAN. A real user management is planned and will cover this.

Until then, publish the port on `127.0.0.1` and reach it over SSH or Tailscale,
or put an authenticating reverse proxy in front. Installing and uninstalling
always needs your own ZimaOS credentials, which zimapp never stores.

## Install on ZimaOS

**[`zimapp-zimaos.yml`](zimapp-zimaos.yml) is ready to install** — image from
Docker Hub (amd64 + arm64), nothing to build:

```bash
git clone https://github.com/chicohaager/zimapp && cd zimapp
pip install pyyaml
ZIMA_HOST=<your-zimaos> ZIMA_USER=<you> ZIMA_PASS='…' \
    python3 zimapp.py install zimapp-zimaos.yml
```

Tile "zimapp" in the grid, WebUI on port 8790. It is an ordinary compose file,
so `docker compose -f zimapp-zimaos.yml up -d` works on any other machine too —
the `x-casaos` block is simply ignored there.

The image is `chicohaager/zimapp:2.0.0` (`:latest` follows it). For a look
without ZimaOS, and without publishing anything to the network:

```bash
docker run --rm -p 127.0.0.1:8790:8790 chicohaager/zimapp:latest
```

⚠️ **`update`, `drift` and the three framework checks are in `main`, not yet in
the released `2.0.0` image.** Whoever installs the image above gets the state of
the tag — the *Compare with installation* button is not in it. From a checkout
everything described here is there; the UI says `v2.1-dev` instead of `v2.0` so
the two cannot be confused.

The bind mount in it is where **Save as blueprint** writes; ZimaOS creates the
directory on first install (measured 2026-07-21: `root:root`, mode 777, so the
container's own user can write in it). Without that mount, saving is switched
off and says so.

> **Note on the ZimaOS UI.** App Store → *Install custom app* has a `YAML` tab,
> but that is a **preview of the form** with a copy button, not a paste field —
> a finished compose cannot be typed into it. (Its own output writes
> `memory: 14,92GB` with a comma, which is exactly what Rule 6 below forbids.)
> The `Import` button next to it was not tested.

### Building the image yourself

Not needed for the released image — this is the path for a modified zimapp, and
it needs no registry account:

```bash
# 1. Put the build context on the host and build it there
ssh youruser@192.168.1.100 'mkdir -p /DATA/AppData/zimapp/src'
scp Dockerfile zimapp*.py youruser@192.168.1.100:/DATA/AppData/zimapp/src/
scp -r static youruser@192.168.1.100:/DATA/AppData/zimapp/src/
ssh youruser@192.168.1.100 'export DOCKER_CONFIG=/DATA/AppData/.dockercfg; \
    cd /DATA/AppData/zimapp/src && docker build -t zimapp:local .'

# 2. Generate own compose and install
python3 zimapp.py convert zimapp-src.yml --name zimapp --title zimapp \
    --category Developer --icon "http://192.168.1.100:8790/icon.svg" \
    --no-icon-check -o zimapp-app.yml           # icon only exists after startup
python3 zimapp.py install zimapp-app.yml
```

Then: **<http://192.168.1.100:8790>**, tile "zimapp" in the ZimaOS grid.

🔴 **After a rebuild, `docker restart` is not enough** — the existing container
keeps running with the old image. A new image needs a recreate.

`update` does not help here: it applies a **changed definition**, and a rebuild
under the same tag changes nothing in the compose — the command stops at "no
differences". Either give the rebuild a new tag (then `update --file` switches
to it, keeping the data directory), or use the loop below.

🔴 **`uninstall` deletes the image the container was created from** — by ID, not
by tag (verified 2026-07-19). So the recreate loop is **rebuild → uninstall →
install**, never the other way round:

- `build → install → uninstall` leaves the tag **gone**, and the next install has
  nothing to start from. There is no error anywhere; the app simply never appears
  in the grid.
- `rebuild → uninstall` moves the tag to a fresh image ID first, so the deletion
  hits the old, now-dangling one and the tag survives.

`install` catches the broken case and names the missing image. Images that come
from a registry are unaffected — the next install just pulls them again.

For apps that hold data, `uninstall` also removes `/DATA/AppData/<app>/`
(`delete_config_folder=true`) — so this loop is only harmless for stateless apps
like zimapp itself.

### What does not work inside the container

`inspect` and `generate` (the single-image path) need SSH to the host — the
image ships no SSH client at all. Converting, validating, the port check and
installing work fully. This applies to both ways of installing above.

## CLI

```bash
export ZIMA_HOST=192.168.1.100
export ZIMA_USER=youruser
export ZIMA_PASS='…'

# compose URL → ZimaOS app (the main path)
python3 zimapp.py convert \
    https://github.com/immich-app/immich/blob/main/docker/docker-compose.yml \
    --name immich --title "Immich" --category Photography \
    --icon "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/immich.svg" -o immich.yml
# (icon.casaos.io has no immich.png — measured 404 on 2026-07-21. The web UI
#  looks an icon up automatically; on the CLI, check the URL before using it.)

# set values for ${VARIABLES} from the source (otherwise: default, otherwise a
# random value for passwords, otherwise an error)
python3 zimapp.py convert <url> --var DB_USERNAME=immich --var TZ=Europe/Berlin

# Dockerfile URL — needs a prebuilt image, ZimaOS does not build
python3 zimapp.py convert <dockerfile-url> --image ghcr.io/dir/app:1.2 --port 8080

# single image (reads EXPOSE/VOLUME/ENV via SSH from the host)
python3 zimapp.py inspect  louislam/uptime-kuma:1
python3 zimapp.py generate louislam/uptime-kuma:1 --title "Uptime Kuma" > kuma.yml

# validate, install, remove
python3 zimapp.py validate  immich.yml
python3 zimapp.py install   immich.yml     # waits and checks; --no-wait skips that
python3 zimapp.py uninstall immich

# change an installed app without uninstalling it
python3 zimapp.py update immich --blueprint immich          # dry run: what would change
python3 zimapp.py update immich --blueprint immich --apply  # and now do it
```

### `update` — changing an app without destroying it

`uninstall` + `install` is not an update: it deletes the app's **images** and its
**data directory**. `update` changes the installation in place
(`PUT /v2/app_management/compose/<app>`), which keeps both.

It starts from what ZimaOS actually has, not from a file lying around here:

1. read the installed compose back from the host,
2. build the new one — from `--source <URL>`, `--blueprint <name>`, a
   `--file`, or the blueprint that carries the app's own name,
3. show what would change, field by field,
4. only with `--apply`: send it and wait until the app really runs it.

Without `--apply` nothing is sent and the exit code is **2 if there are
differences**, 0 if there are none — that is the drift check for a cron job.

The **web UI has the same two steps** in section 7: *Compare with installation*
reads what ZimaOS really has and lists the differences without sending anything,
and *Apply update* appears only after that. It applies **exactly the text that
was compared** — edit the compose in between and it refuses, because the list on
screen then describes a different file.

```text
5 difference(s) between the installation and the new definition:
  ~ services.webserver.image
      'ghcr.io/paperless-ngx/paperless-ngx:2.14'  ->  'ghcr.io/paperless-ngx/paperless-ngx:2.15'
  ~ x-casaos.tagline.en_us
      'Dokumentenarchiv mit OCR'  ->  'Document archive with OCR'
```

**Values that already run are kept.** A regenerated `POSTGRES_PASSWORD` against
an existing database volume means the app never comes up again, so anything
zimapp *generated* is taken from the installation instead. Values a source or a
blueprint states explicitly stay a visible difference — `--keep NAME` keeps
those too, `--var NAME=VALUE` overrides.

🔴 **What ZimaOS reports about an update cannot be believed** (measured
2026-07-21). A `PUT` whose image cannot be pulled answers `HTTP 200`, appears in
the stored compose — in one run for 7 seconds, in the next for over 21 — and the
app keeps reporting `running` the whole time, because the old container never
stopped. The app grid is no second opinion: its `image` field flips along with
the stored file. `update` therefore waits for
`GET /v2/app_management/compose/<app>/containers`, which reports the image a
container actually runs, and fails loudly when the app is still on the old one:

```text
  RUNNING: app: runs chicohaager/zimapp:2.0.0, should run chicohaager/zimapp:1.9
ERROR: The app does not run the new definition. Nothing was destroyed — the old
container is still up, which is exactly why neither the app status nor the
stored compose shows a problem.
```

Not compared, because the API does not return it: the per-service `x-casaos`
block (port and volume descriptions) and `x-casaos.store_app_id`. The command
says so every time rather than quietly leaving it out.

### After the install

`install` does not stop at the API's "accepted". It polls the app grid until the
app reports `running`, then probes the port, and — if a blueprint of that name
exists — runs its expectations:

```text
Waiting for the app (HTTP 200 means accepted, not done)…
  t+   0s  not in the grid yet
  t+   3s  running
  [ok  ] app in the grid — status 'running' after 3s
  [ok  ] reachable at http://192.168.1.100:8790 — HTTP 200
```

When something fails, the check names the cause instead of the symptom:

| Symptom | What it reports |
| --- | --- |
| app never appears | which referenced images are missing on the host (and that `uninstall` deleted them) |
| port unreachable, ZFW active | open the port, then `zfw apply` **and** `zfw commit` — without apply the rule does nothing (§13.2.2) |
| port unreachable, no ZFW | not the firewall — check `docker logs` |
| no SSH for the diagnosis | says so, instead of guessing |

## Blueprints — and what "tested" is allowed to mean

A blueprint is **not** a stored compose file. It holds only what cannot be
derived from upstream, plus the expectations that get executed against the
running installation:

```bash
python3 zimapp.py blueprints                          # catalogue + how old each proof is
python3 zimapp.py convert --blueprint paperless-ngx -o app.yml
python3 zimapp.py install app.yml
python3 zimapp.py verify paperless-ngx                # runs the expect block live
```

Why a delta and not a copy: upstream changes image tags, volume paths and env
names. A stored compose rots silently and hands out a "tested" file months later
that no longer matches reality. A blueprint pins the source (`pin: <commit>`) and
stores only the difference.

What the converter cannot know, and a blueprint supplies — all of it observed on
paperless-ngx: upstream's `env_file` contains nothing but comments, so the stack
starts with **no admin account**; `PAPERLESS_URL` must carry host *and* port or
Django rejects the login POST (§4.4.2); the secret key defaults to a published
value.

`${generate:N}`, `${host}`, `${port}`, `${app}` and `${scheme}` are expanded
**after** the conversion — only then is the assigned host port known, and
`PAPERLESS_URL` needs exactly that one, not the port upstream happened to use.

### Saving one from the web UI

**Save as blueprint** in step 6 stores the current form — source URL, metadata,
the values that were typed. Not the generated compose: a stored compose is the
copy this whole section argues against.

Set `ZIMAPP_BLUEPRINT_DIR` to a writable directory and mount it into the
container; without it the button says saving is off, because the directory in the
image is read-only and disappears with the next container recreate:

```yaml
    environment:
      - ZIMAPP_BLUEPRINT_DIR=/blueprints
    volumes:
      - type: bind
        source: /DATA/AppData/zimapp/blueprints   # any path — an encrypted folder works
        target: /blueprints
```

The directory has to be writable **by the uid the container runs as** (1000
here), not by the user who created it. Saved blueprints carry **no `verified:`
block** — that field is for what was actually observed, so it stays absent until
`zimapp verify` has run and something really held. Files are written 0660, and
values that look like secrets are named after saving; `${generate:24}` instead of
a literal password stores nothing sensitive at all.

### Drift: has upstream moved?

A blueprint pins what upstream looked like when someone proved it — as a
**fingerprint**, not as a copy:

```bash
python3 zimapp.py drift                 # all blueprints
python3 zimapp.py drift paperless-ngx
```

It fetches every source, compares its sha256 against `verified.source_sha256`,
re-converts and validates the result. Exit code **0** unchanged, **2** moved or
never fingerprinted, **1** a recipe that no longer converts.

Why a hash and not the generated compose: a stored copy is exactly what makes a
catalogue rot. A fingerprint holds no content — it answers one question, "is this
still the file someone looked at", and that is the question a "tested" badge
cannot answer for itself.

🔴 **This needs the network, not a ZimaOS host — and therefore says nothing
about any installation.** `.github/workflows/blueprint-drift.yml` runs it weekly
and opens (or comments on) an issue, with that limitation written into the issue
text. The live half is `zimapp.py update <app> --blueprint <name>` (exit 2 on
drift) and `zimapp.py verify <name>`.

### The expect block

```yaml
expect:
  - http: /accounts/login/
    status: 200
    contains: [csrfmiddlewaretoken, 'name="login"']   # the form really rendered
    absent: [Internal Server Error]
    min_bytes: 500
  - http: /api/ui_settings/
    status: 401        # API is up and enforcing auth
  - http: /
    status: 302        # unauthenticated root redirects
```

Assertions are on the **payload**, not just the status code — an app answering
200 with an empty body is broken, and a status-only check would call it healthy.
Redirects are **not** followed by default (`follow: true` opts in), otherwise a
302 would silently arrive as the 200 of its target.

`verify` exits non-zero when an expectation fails, so it works in CI. Each
blueprint records what was actually observed and when:

```yaml
verified:
  date: 2026-07-19
  host: ZimaOS v1.7.0-beta1 (192.168.1.100)   # NB: 'host', not 'on' — see below
  result: >-
    Converted, installed, all three containers up, webserver healthy, tile in
    the grid, superuser login proven via POST (302).
```

⚠️ Two YAML traps that bit us here: a bare `on:` is the **boolean True** in
YAML 1.1 (so `verified.on` is unreadable by name), and an unquoted `2026-07-19`
becomes a `datetime.date` that `json.dumps` refuses — which took down
`/api/defaults` with an empty response. Both are covered by tests now.

## What the converter translates

| From the source | Becomes |
| --- | --- |
| multiple services | one app; `x-casaos.main` = WebUI service, all in one bridge network |
| `ports: ["8080:80"]` | long form with `published: "8080"` (string!) + `port_map` |
| host port taken or reserved by ZimaOS | next free port, with a note (see "Port check" below) |
| named volume `pgdata:` | bind to `/DATA/AppData/<app>/pgdata` |
| relative bind `./data` | bind to `/DATA/AppData/<app>/data` |
| absolute bind `/srv/app/data` | bind to `/DATA/AppData/<app>/data`, every move named in the notes — ZimaOS' root is read-only, docker cannot create the directory and the container would not start |
| `/media/*`, `/mnt/*` | stays unchanged (real host locations) |
| `/var/run/docker.sock`, `/dev/*`, `/etc/localtime` | stays unchanged (socket/device/clock) |
| `env_file: .env` | the file next to the source is fetched, values move into `environment` |
| `${DB_PASSWORD}` without a default | random value, reported in cleartext |
| `${FOO:-bar}` | `bar` |
| `${FOO:-bar}` **and** `FOO=baz` in the env file | `baz` — the env file wins, exactly as compose reads a `.env`. The form shows `baz` and names the default it overrides |
| a value typed into the field | wins over both, and lands in `image:` **and** `environment:` |
| `build:` | error — ZimaOS only installs prebuilt images (§4.4.1) |
| image name | app ID (`immich-server` → `immich`), title, directory name |

The WebUI service is not guessed, but derived: services with known
infrastructure images (postgres, redis, valkey, meilisearch …) are not eligible
for it. Can be overridden manually via `--main` or the dropdown.

### Port check

`--check-ports` reads the taken host ports from two sources and uses whichever
answers. Neither is complete on its own, so the output always names the source:

| Source | Needs | Sees | Blind to |
| --- | --- | --- | --- |
| app grid API (`--port-source api`) | credentials | ZimaOS-managed apps | foreign containers, host-native services |
| docker via SSH (`--port-source ssh`) | key login | every published container port | host-native services |

`auto` (the default) tries both. Ports ZimaOS occupies itself (22, 80, 443,
7681 …) are hardcoded and always avoided. If **no** source works, that is an
error — a port check that silently returns "nothing taken" is worse than none.

The API path is what makes the containerised zimapp useful: the image has no
SSH client, so `ssh` degrades to a visible note and the API carries the check.

## The rules the validator enforces

All verified on the live system, not copied from the docs:

1. **Auth:** the JWT belongs **raw** in the `Authorization` header. With the prefix `Bearer` including the space
   ZimaOS answers `invalid or expired jwt` — looks like a token problem,
   but is a header format problem.
2. **Install:** `POST /v2/app_management/compose`, `Content-Type: application/yaml`,
   body = raw compose.
3. **Uninstall:** `DELETE /v2/app_management/compose/{name}?delete_config_folder=true`
4. **Top-level `name:`** is mandatory — that is the app ID and the directory name.
5. 🔴 **`port_map` MUST be a string.** An int breaks the parser, and the app
   then disappears **silently** from the grid. Also applies to `published:`.
6. 🔴 **Decimal numbers need a period.** `memory: 14,92GB` → HTTP 400
   (`strconv.ParseFloat`). See "Known ZimaOS bug" below.
7. **Persistent data to `/DATA/AppData/<app>/`.** Drive-letter paths
   (`/media/sdb/…`) break after a reboot — use UUID-based paths.
   🔴 Absolute binds outside `/DATA` are moved under `/DATA/AppData/<app>/`,
   and each move is named. Two different reasons, both measured on 1.7.0-beta1:
   `/srv` and `/usr/local` are **not creatable** (root mounted read-only, docker
   fails at `mkdir /srv: read-only file system` and the container never starts),
   while `/etc`, `/opt` and `/var/lib` *are* writable as root but lie outside
   the app data area and are not persistence-safe. Untouched: `/media`, `/mnt`,
   sockets, devices, `/etc/localtime`, `/etc/timezone` and `/var/lib/docker`
   (the daemon's own state — an app mounting it means exactly that directory).
   If the source path already holds data, copy it over by hand.
8. **`icon` must be a reachable URL**, otherwise the tile stays empty.
   The validator checks this with a HEAD request and falls back to GET, because
   many small servers answer HEAD with 501. ⚠️ The CasaOS URL that reads like the
   obvious default, `…/all/default.png`, **404s** (checked 2026-07-19).
   🔴 **Reachable is not the same as right.** `…/all/box.png` resolves, so it
   passes this check — and puts the **Box.com logo** on the tile. It was the
   form's prefilled default until 2026-07-21 and shipped an Immich install that
   way. The field is empty now; after *Analyze*, zimapp looks the app id up at
   `icon.casaos.io`, then at `selfh.st/icons`, and only fills in what really
   answers. Nothing found → the field stays empty and says so. An empty tile is
   visible; a foreign logo looks like it worked.

Additionally validated: `x-casaos.main` points to an existing service,
`port_map` really is one of its published ports, no duplicate or
ZimaOS-reserved host ports, no `build:`, no `env_file:`, no
relative binds, no unresolved `${VAR}`, no `depends_on` pointing at nothing.

### Three traps that leave the app *running* and unusable

ZimaOS sees none of these: the container is up, the port answers, the tile is
there. They only show when someone tries to log in.

- **The app is told an address it is not reachable on.** A variable like
  `PAPERLESS_URL` / `APP_URL` naming port 8000 while the service publishes 8123
  → an app with an origin check (Django, Rails and friends) serves the page and
  rejects the login POST (§4.4.2). Correct as it stands only behind a reverse
  proxy — the warning says so.
- **A secret that is a well-known placeholder** (`change_me`, `secret`,
  `password`…) is an error: everyone who read the upstream compose knows it.
  An **empty** secret is only a warning, because whether it is fatal cannot be
  read off the file — measured on a live pterodactyl, where `MAIL_PASSWORD` is
  empty on purpose and everything works. Crying wolf here would teach people to
  ignore the whole class.
- **Half an admin account.** `*_ADMIN_USER` without `*_ADMIN_PASSWORD` (or the
  other way round): apps that create their superuser on first start create none,
  the app runs, and nobody gets in.

Each of these reads its evidence out of the compose file and quotes the value it
read. None of them guesses a framework from the image name — "looks like Django"
is not a measurement, and a warning built on it is noise on everything it gets
wrong. Measured against the six apps running here: zero false alarms, one honest
warning (that empty `MAIL_PASSWORD`).

## Known ZimaOS bug (v1.7.0-beta1)

The built-in "Benutzerdefinierte App installieren" UI (that is the label a German
browser locale shows — which is the whole point here) is **unusable in a German
browser locale**. It generates the memory limit with a
decimal comma (`memory: 14,92GB`), the Go backend parses it with
`strconv.ParseFloat` and rejects every installation:

```text
1 error(s) decoding:
* error decoding 'deploy.resources.limits.memory':
  strconv.ParseFloat: parsing "14,92": invalid syntax
```

Reproduced with two identical payloads that differ only in the separator:
comma → HTTP 400, period → HTTP 200.

`zimapp` is not affected by this, because it always writes values with a period
and validates that before sending.

## Tests

```bash
python3 -m unittest -v test_zimapp     # 175 tests, without network and without host
```

Every test pins down a rule that has caused trouble before. Whoever changes a
rule has to come by there.

## Limitations

- **A Dockerfile is not built.** ZimaOS only installs prebuilt images
  (§4.4.1), which is why the Dockerfile source needs an `--image`. From the
  Dockerfile only `EXPOSE`/`VOLUME`/`ENV` are taken.
- **Network aliases and multiple networks are merged into one bridge
  network.** Service-to-service DNS via the service name is preserved;
  whoever builds on aliases has to rework it.
- **`docker inspect` runs on the ZimaOS host** via SSH — key login must be
  set up there (our ZimaOS notes §26.3/§26.4). Only `inspect`,
  `generate` and `--check-ports` need that; `convert` works without a host.
- **Do not forget after the deploy:** open the ZFW port, otherwise LAN timeout
  despite running containers (§13.2.2).

## Files

| File | Content |
| --- | --- |
| `zimapp.py` | CLI + the eight rules as a comment |
| `zimapp_core.py` | converter: fetch, detect, variables, rewrite, validate, API |
| `zimapp_web.py` | HTTP server and JSON API of the web UI |
| `static/` | UI (`index.html`, `app.js`, `styles.css` in the ZFW color scheme) |
| `test_zimapp.py` | regression tests |
| `blueprints/` | tested recipes: pinned source, the values an app needs, expectations |
| `zimapp-zimaos.yml` | ready-to-install ZimaOS compose on the released image |
| `Dockerfile`, `zimapp-src.yml` | build and deployment as a ZimaOS app |
| `docs/` | the screenshots in this README |
| `.github/workflows/` | the weekly blueprint drift check |
