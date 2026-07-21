#!/usr/bin/env python3
"""
zimapp_core — the converter behind the CLI and the web UI.

The input is a URL (or file) pointing to a compose file or a Dockerfile;
the output is a ZimaOS-compliant compose with an x-casaos block. Multi-service
stacks (app + database) stay ONE stack in the process: x-casaos.main points at
the WebUI service, all services hang off a shared bridge network, and all
volumes end up under /DATA/AppData/<app>/ (ZIMAOS-KNOWLEDGE.md §3.4, §4.4.1).

The rules that ZimaOS acknowledges silently or cryptically are listed as RULES
in zimapp.py and are enforced here resp. checked in validate().
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # fail loudly, not silently (no fallback guessing)
    print("ERROR: PyYAML is missing. Install it with: pip install pyyaml", file=sys.stderr)
    raise

INSTALL_PATH = "/v2/app_management/compose"
LOGIN_PATH = "/v1/users/login"

VALID_CATEGORIES = {
    "Backup", "Cloud", "Developer", "Documents", "Entertainment", "Finance",
    "Games", "Home Automation", "Media", "Networking", "Photography",
    "Productivity", "Security", "Social", "Utilities",
}

# Ports that ZimaOS occupies itself — never propose them as a host port.
# Source: ZIMAOS-KNOWLEDGE.md §8.3 + ZFW default whitelist §13.2.2.
RESERVED_PORTS = {22, 80, 139, 443, 445, 1910, 3702, 5355, 7681, 8200, 9527, 9993, 11010}

# Images that are typically infrastructure and NEVER provide the WebUI.
# Only used to pick the main service; recognised by the image name without registry/tag.
SUPPORTING_IMAGES = {
    "postgres", "postgis", "mysql", "mariadb", "mongo", "mongodb", "redis",
    "valkey", "keydb", "memcached", "clickhouse-server", "rabbitmq", "nats",
    "elasticsearch", "opensearch", "meilisearch", "typesense", "qdrant",
    "solr", "influxdb", "cassandra", "couchdb", "etcd", "zookeeper", "kafka",
    "minio", "mailhog", "maildev", "chromadb", "pgbouncer", "pgvector",
}

# Paths that must NOT be rewritten to /DATA/AppData: sockets and devices have to
# stay where the kernel has them, and /var/lib/docker is the daemon's own state —
# an app that mounts it (wings reads container logs there) means exactly that
# directory. Docker Root Dir measured on .147: /var/lib/docker.
# /tmp is a real writable tmpfs on the host (measured: rw,nosuid,nodev) — a bind
# there wants scratch space, and moving it to /DATA would make throwaway data
# persistent and eat disk. It also breaks apps that need the path to be identical
# inside and outside the container (wings hands it to the docker daemon).
PASSTHROUGH_PREFIXES = ("/var/run/", "/run/", "/dev/", "/sys/", "/proc/",
                        "/etc/localtime", "/etc/timezone", "/var/lib/docker", "/tmp/")

# Absolute paths that really exist on a ZimaOS box and that an app may legitimately
# want — disks and mount points.
HOST_PATH_PREFIXES = ("/media/", "/mnt/")

# Kept where they are, but empty after every reboot — worth its own warning.
TMPFS_PREFIXES = ("/tmp/", "/run/", "/var/run/")

# Everything else absolute is moved under /DATA/AppData. Careful with the reason:
# it is NOT the same everywhere, measured on 1.7.0-beta1 (2026-07-21):
#   /srv          → does not exist, `/` is mounted ro → `mkdir /srv: read-only
#                   file system`, docker cannot create the bind source, the
#                   container never starts.
#   /usr/local    → read-only as well.
#   /etc, /opt,
#   /var/lib      → writable as root. Docker CAN create a directory there — it is
#                   simply the wrong place: outside /DATA nothing is persistence-
#                   safe (Rule 7).
# Both cases want the same fix, so the message must not claim the hard reason when
# it might be the soft one.

# Variable names for which we are allowed to generate a secret on request.
SECRET_HINTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "salt", "_key")

# --- Framework traps --------------------------------------------------------
#
# Three ways an app comes up healthy and is still unusable. All three were hit
# on the live system, and none of them is visible to ZimaOS: the container is
# running, the port answers, the tile is there.
#
# What matters about these checks: every one of them reads its evidence out of
# the compose file itself. None of them guesses from the image name what
# framework might be inside — "looks like Django" is not a measurement, and a
# warning built on it would be noise on every app it guesses wrong.

# Variables through which an app is told its own external address. If one of
# these exists, the app builds links or an origin check from it — and then the
# value has to match the port the app is actually reachable on.
PUBLIC_URL_HINTS = ("_url", "_origin", "_origins", "_host", "_hosts", "_domain",
                    "_site", "site_url", "public_url", "external_url", "base_url")

# Values that are in a compose only because nobody replaced them. A generated
# secret is fine; these are the ones everybody on the internet knows.
PLACEHOLDER_VALUES = {
    "change_me", "changeme", "change-me", "please_change", "please-change-me",
    "secret", "mysecret", "my_secret", "supersecret", "secretkey", "secret_key",
    "password", "passwd", "pass", "admin", "root", "example", "test",
    "your_secret_key", "your-secret-key", "yoursecretkey", "changeit",
    "insecure", "todo", "xxx", "1234", "12345", "123456", "password123",
}

# Pairs that only work together: a user without a password is an app nobody can
# log into, and the installation reports no problem whatsoever.
ADMIN_USER_HINTS = ("admin_user", "admin_username", "admin_name", "superuser",
                    "admin_email", "initial_admin_user")
ADMIN_PASSWORD_HINTS = ("admin_password", "admin_pass", "superuser_password",
                        "initial_admin_password")


# --- Helpers ----------------------------------------------------------------

class ConvertError(Exception):
    """Domain error with a message that is fit to show directly in the UI/CLI."""


def slugify(text):
    """App ID: lowercase, only a-z0-9 and hyphens (ZimaOS uses it as a directory name)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug or "app"


def image_basename(image):
    """'ghcr.io/immich-app/immich-server:v1.2' -> 'immich-server'"""
    if not image:
        return ""
    ref = image.split("@")[0]
    path = ref.rsplit("/", 1)[-1]
    return path.rsplit(":", 1)[0].lower()


def app_name_from_image(image, fallback=""):
    """Derive an app name from the image: 'immich-server' -> 'immich'.

    The role suffix of the WebUI container belongs to the stack, not to the app —
    as a directory name under /DATA/AppData, 'immich-server' would be misleading,
    because the data of the ENTIRE stack lives there.
    """
    base = image_basename(image) or fallback
    return re.sub(r"[-_](server|app|web|frontend|backend|core|main)$", "", base) or base


# --- Fetch the source -------------------------------------------------------

def _raw_url(url):
    """Turn GitHub/GitLab blob URLs into their raw variant.

    A blob link returns HTML instead of YAML; that is by far the most common
    copy & paste case, which is why we correct it instead of failing on it.
    """
    p = urllib.parse.urlsplit(url)
    if p.netloc == "github.com" and "/blob/" in p.path:
        return urllib.parse.urlunsplit(
            ("https", "raw.githubusercontent.com", p.path.replace("/blob/", "/", 1), "", "")
        )
    if p.netloc == "gitlab.com" and "/-/blob/" in p.path:
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.replace("/-/blob/", "/-/raw/", 1), p.query, ""))
    return url


def fetch_source(url, timeout=30):
    """Fetch a compose file/Dockerfile from a URL or a local path.

    Returns (text, effective_url). Errors are raised loudly — an empty
    response is an error, not an empty result.
    """
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        if not os.path.exists(url):
            raise ConvertError(f"Neither a URL nor an existing file: {url}")
        with open(url, encoding="utf-8") as fh:
            text = fh.read()
        if not text.strip():
            raise ConvertError(f"File {url} is empty.")
        return text, url

    effective = _raw_url(url)
    scheme = urllib.parse.urlsplit(effective).scheme
    if scheme not in ("http", "https"):
        raise ConvertError(f"Unsupported scheme '{scheme}://' — only http/https.")

    req = urllib.request.Request(effective, headers={"User-Agent": "zimapp"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4 * 1024 * 1024)
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise ConvertError(f"HTTP {e.code} while loading {effective}") from e
    except urllib.error.URLError as e:
        raise ConvertError(f"{effective} not reachable: {e.reason}") from e

    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise ConvertError(f"{effective} returned an empty body.")
    if "html" in ctype.lower() or text.lstrip().lower().startswith("<!doctype html"):
        raise ConvertError(
            f"{effective} returns HTML, not YAML/Dockerfile. For GitHub/GitLab use the "
            f"raw link (github.com/.../blob/... is turned automatically)."
        )
    return text, effective


def detect_kind(text):
    """'compose' | 'dockerfile' — based on the content, not on the file name."""
    if re.search(r"^\s*(FROM|ARG)\s+\S", text, re.M) and not re.search(r"^\s*services\s*:", text, re.M):
        return "dockerfile"
    error = None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        doc, error = None, e
    if isinstance(doc, dict) and isinstance(doc.get("services"), dict):
        return "compose"
    if re.search(r"^\s*FROM\s+\S", text, re.M):
        return "dockerfile"
    if error is not None and re.search(r"^\s*services\s*:", text, re.M):
        # It IS a compose file, it just does not parse. Saying "not a compose
        # file" here sends people looking for the wrong thing entirely — it
        # happens with upstream files too (pterodactyl/wings' own example has a
        # tab-indented line, and tabs are illegal for YAML indentation).
        raise ConvertError(
            f"This has a 'services:' block, but the YAML does not parse: {error}"
        )
    raise ConvertError(
        "The content is neither a compose file (top-level 'services:') nor a "
        "Dockerfile (a line with 'FROM …')."
    )


# --- Dockerfile -------------------------------------------------------------

def parse_dockerfile(text):
    """Read EXPOSE / VOLUME / ENV / FROM from a Dockerfile.

    Purely static: the Dockerfile is NOT built. ZimaOS only installs
    prebuilt images (§4.4.1 — 'build:' is not allowed in the app directory),
    which is why this function only returns the metadata; the image has to be
    named separately.
    """
    lines, buf = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        lines.append(buf + line)
        buf = ""
    if buf:
        lines.append(buf)

    ports, volumes, env, base = [], [], [], None
    for line in lines:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, rest = parts[0].upper(), parts[1].strip()
        if key == "FROM" and base is None:
            base = rest.split()[0]
        elif key == "EXPOSE":
            for tok in rest.split():
                port, _, proto = tok.partition("/")
                if proto and proto.lower() != "tcp":
                    continue
                if port.isdigit():
                    ports.append(int(port))
        elif key == "VOLUME":
            if rest.startswith("["):
                try:
                    volumes += [str(v) for v in json.loads(rest)]
                except json.JSONDecodeError:
                    pass
            else:
                volumes += rest.split()
        elif key == "ENV":
            if "=" in rest:
                # ENV A=1 B=2  — naively splitting on the space is enough for the
                # common case; values containing spaces stay with the first pair.
                for chunk in re.findall(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)", rest):
                    env.append(f"{chunk[0]}={chunk[1].strip(chr(34)+chr(39))}")
            else:
                k, _, v = rest.partition(" ")
                if k:
                    env.append(f"{k}={v.strip()}")

    return {
        "base": base,
        "ports": sorted(set(ports)),
        "volumes": sorted(set(volumes)),
        "env": [e for e in env if not e.startswith("PATH=")],
    }


def compose_from_dockerfile(meta, image, service_name="app"):
    """Build a minimal compose document from Dockerfile metadata."""
    svc = {"image": image}
    if meta["ports"]:
        svc["ports"] = [f"{p}:{p}" for p in meta["ports"]]
    if meta["volumes"]:
        svc["volumes"] = list(meta["volumes"])
    if meta["env"]:
        svc["environment"] = list(meta["env"])
    svc["restart"] = "unless-stopped"
    return {"services": {service_name: svc}}


# --- Variables --------------------------------------------------------------

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-?]([^}]*))?\}")


def parse_dotenv(text):
    """Read KEY=VALUE from a .env file (ignoring comments and 'export')."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def check_icon(url, timeout=10):
    """Check whether the icon URL really delivers something (Rule 8).

    Otherwise a dead icon is only noticed once the tile stays empty in the grid —
    ZimaOS reports nothing about it. Returns None if everything is fine,
    otherwise the reason in plain text.
    """
    if not url:
        return "no icon set"
    if not re.match(r"^https?://", url):
        return f"'{url}' is not an http(s) URL"
    # HEAD first (saves the download), but many small servers do not implement
    # it and answer 501/405. That does NOT mean "icon broken" — in that case
    # follow up with GET, otherwise the check reports healthy icons as dead.
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": "zimapp"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status < 400:
                    return None
                return f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (405, 501):
                continue
            return f"HTTP {e.code}"
        except urllib.error.URLError as e:
            return f"not reachable ({e.reason})"
        except OSError as e:
            return f"not reachable ({e})"
    return None


# Where an icon can be looked up automatically, in the order they are tried.
# icon.casaos.io first because that is the source ZimaOS' own store uses and it
# matches the grid look — but it only covers a fraction of the apps: immich,
# uptime-kuma, vaultwarden and paperless-ngx are all 404 there (measured
# 2026-07-21), which is why selfh.st follows. SVG before PNG (§5.4).
ICON_SOURCES = (
    "https://icon.casaos.io/main/all/{app_id}.png",
    "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/{app_id}.svg",
    "https://cdn.jsdelivr.net/gh/selfhst/icons/png/{app_id}.png",
)


def suggest_icon(app_id, timeout=6):
    """Look for an icon that really exists for this app id (Rule 8).

    Returns (url, tried); url is None when no source answers, and 'tried' lists
    (url, reason) for every attempt so the UI can say what was looked at.

    Deliberately without a generic placeholder as a last resort: a placeholder
    is reachable, so it passes check_icon, and then sits in the grid as somebody
    else's brand — that is how an Immich tile ended up showing the Box.com logo.
    An empty field is honest, a foreign logo is not.
    """
    tried = []
    if not app_id:
        return None, tried
    for pattern in ICON_SOURCES:
        url = pattern.format(app_id=app_id)
        reason = check_icon(url, timeout=timeout)
        tried.append((url, reason))
        if reason is None:
            return url, tried
    return None, tried


def env_file_names(text):
    """Collect all env_file references of a compose document.

    Both spellings occur (`env_file: .env` and a list), and the value can also
    be a mapping with 'path' — which is why we go via the parsed document
    instead of using a regex.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return [".env"] if "env_file:" in text else []
    if not isinstance(doc, dict):
        return []

    names = []
    for svc in (doc.get("services") or {}).values():
        entry = (svc or {}).get("env_file")
        if not entry:
            continue
        items = entry if isinstance(entry, list) else [entry]
        for item in items:
            if isinstance(item, dict):
                item = item.get("path")
            if isinstance(item, str):
                names.append(item)
    return names or ([".env"] if "env_file:" in text else [])


def fetch_env_files(source_url, names):
    """Find the matching file next to the source for an env_file reference.

    A ZimaOS app is ONE compose file — an `env_file: .env` points at
    something that does not exist on the host. Instead of leaving that
    silently broken, we fetch the file (or its .example) from the same
    directory as the source and write the values straight into `environment:`.

    Returns (values, used_url_or_None).
    """
    if not re.match(r"^https?://", source_url or ""):
        return {}, None

    base = source_url.rsplit("/", 1)[0] + "/"
    candidates = []
    for name in names:
        stem = name.lstrip("./")
        candidates += [stem, f"{stem}.example", f"{stem}.sample", f"{stem}.template"]
    candidates += ["example.env", ".env.example", ".env.sample"]

    seen, empty_hit = set(), None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            text, used = fetch_source(urllib.parse.urljoin(base, candidate), timeout=15)
        except ConvertError:
            continue
        values = parse_dotenv(text)
        if values:
            return values, used
        # The file exists but contains only comments (which happens often with
        # example .envs) — that is something different from "not found"
        # and is therefore reported separately.
        empty_hit = empty_hit or used
    return {}, empty_hit


def find_variables(text):
    """Collect all ${VAR} / ${VAR:-default} in the raw text.

    Compose files from the network almost always expect a .env that ZimaOS
    does not ship. Unresolved variables are therefore a blocker, not a
    cosmetic flaw — they are made visible here.
    """
    found = {}
    for name, default in VAR_RE.findall(text):
        entry = found.setdefault(name, {"name": name, "default": None, "secret": False})
        if default and entry["default"] is None:
            entry["default"] = default
        entry["secret"] = any(h in name.lower() for h in SECRET_HINTS)
    return [found[k] for k in sorted(found)]


def resolve_variables(text, values, autofill_secrets=True):
    """Replace ${VAR}: user value first, then the default, then possibly a secret.

    Returns (text, generated) — generated lists the generated secrets that the
    user MUST see (otherwise nobody knows the DB password).
    """
    generated = {}

    def sub(match):
        name, default = match.group(1), match.group(2)
        if values.get(name):
            return str(values[name])
        if default:
            return default
        if autofill_secrets and any(h in name.lower() for h in SECRET_HINTS):
            value = generated.get(name) or secrets.token_urlsafe(18)
            generated[name] = value
            return value
        return match.group(0)  # leave unresolved → validate() complains loudly

    return VAR_RE.sub(sub, text), generated


# --- Host state -------------------------------------------------------------

DOCKER_CONFIG_DIR = os.environ.get("ZIMA_DOCKER_CONFIG", "/DATA/AppData/.dockercfg")


def ssh_docker(host, ssh_user, args, docker_config=None):
    """Call docker on the ZimaOS host.

    DOCKER_CONFIG deliberately does NOT point at /DATA/.docker — that one is
    root-only and makes every non-root call fail with 'permission denied' (§3.6).
    """
    remote = (f"export DOCKER_CONFIG={docker_config or DOCKER_CONFIG_DIR}; docker "
              + " ".join(args))
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{ssh_user}@{host}", remote],
            capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        # No ssh binary at all — that is the normal state inside the container
        # image. It has to become a ConvertError, otherwise it escapes every
        # caller that degrades gracefully on one source failing, and takes the
        # whole request down with a 500.
        raise ConvertError(
            "no 'ssh' client available in this environment (that is normal inside the "
            "zimapp container) — use the app grid API instead, it needs credentials but no SSH."
        ) from e
    except OSError as e:
        raise ConvertError(f"ssh to {host} could not be started: {e}") from e
    if proc.returncode != 0:
        raise ConvertError(f"docker on {host} failed:\n{proc.stderr.strip()}")
    return proc.stdout


def used_host_ports(host, ssh_user):
    """Every published host port, read from docker on the host.

    This is the complete picture — it includes containers ZimaOS knows nothing
    about — but it needs an SSH key login, which a containerised zimapp does
    not have.
    """
    out = ssh_docker(host, ssh_user, ["ps", "-a", "--format", "{{.Ports}}"])
    return {int(m) for m in re.findall(r"0\.0\.0\.0:(\d+)", out)}


def used_app_ports(host, user, password):
    """Host ports of the apps ZimaOS manages, via the app grid API.

    Needs no SSH, so this also works from inside a container. The blind spot is
    real and deliberate: the API only reports ZimaOS *apps*. Containers started
    by hand and host-native services (SSH, Samba, the ZimaOS gateway itself) do
    not appear here — those are covered by RESERVED_PORTS and, if available, by
    used_host_ports().
    """
    token = login(host, user, password)
    status, raw = api(host, "GET", "/v2/app_management/web/appgrid", token)
    if status != 200:
        raise ConvertError(f"App grid on {host} returned HTTP {status}: {raw[:200]}")
    try:
        apps = json.loads(raw)["data"] or []
    except (KeyError, ValueError) as e:
        raise ConvertError(f"Unexpected app grid response from {host}: {raw[:200]}") from e

    ports = set()
    for app in apps:
        value = str((app or {}).get("port") or "").strip()
        if value.isdigit():
            ports.add(int(value))
    return ports


def collect_used_ports(host, ssh_user=None, user=None, password=None, sources=("api", "ssh")):
    """Gather taken host ports from every source that is actually usable.

    Returns (ports, notes). A source that fails becomes a NOTE, not silence —
    a half-done port check that pretends to be complete is how you end up
    proposing a port that is already taken.
    """
    ports, notes, worked = set(), [], []

    if "api" in sources and user and password:
        try:
            found = used_app_ports(host, user, password)
            ports |= found
            worked.append("app grid API")
            notes.append(f"App grid API: {len(found)} port(s) in use by ZimaOS apps "
                         f"(does not see foreign containers or host services).")
        except ConvertError as e:
            notes.append(f"App grid API unusable: {e}")
    elif "api" in sources:
        notes.append("App grid API skipped: no credentials given.")

    if "ssh" in sources and ssh_user:
        try:
            found = used_host_ports(host, ssh_user)
            ports |= found
            worked.append("docker via SSH")
            notes.append(f"docker via SSH: {len(found)} published port(s) — complete host view.")
        except ConvertError as e:
            notes.append(f"docker via SSH unusable: {e}")

    if not worked:
        raise ConvertError(
            "Port check requested, but no source worked:\n- " + "\n- ".join(notes) +
            "\nEither pass credentials (app grid API) or set up an SSH key login."
        )
    return ports, notes


def inspect_image(image, host, ssh_user):
    """Read EXPOSE/VOLUME/ENV of an image (pulls it onto the host if needed)."""
    if image not in ssh_docker(host, ssh_user, ["images", "--format", "{{.Repository}}:{{.Tag}}"]):
        ssh_docker(host, ssh_user, ["pull", image])

    cfg = json.loads(ssh_docker(host, ssh_user, ["inspect", image]))[0].get("Config", {}) or {}
    ports = sorted(
        int(p.split("/")[0]) for p in (cfg.get("ExposedPorts") or {}) if p.split("/")[1] == "tcp"
    ) if cfg.get("ExposedPorts") else []
    return {
        "ports": ports,
        "volumes": sorted((cfg.get("Volumes") or {}).keys()),
        "env": [e for e in (cfg.get("Env") or []) if not e.startswith("PATH=")],
        "entrypoint": cfg.get("Entrypoint"),
        "cmd": cfg.get("Cmd"),
        "labels": cfg.get("Labels") or {},
    }


# --- Ports ------------------------------------------------------------------

def pick_host_port(preferred, used):
    """Pick a free host port: preferably the requested one, otherwise counting upwards."""
    candidate = preferred if preferred and preferred >= 1024 else 8080
    while candidate in RESERVED_PORTS or candidate in used or candidate < 1024:
        candidate += 1
    used.add(candidate)
    return candidate


def normalize_ports(entries, used, warnings, service):
    """Bring short and long syntax into the ZimaOS long form.

    published is ALWAYS a string (Rule 5) and is chosen free of collisions.
    """
    result = []
    for entry in entries or []:
        if isinstance(entry, dict):
            target = entry.get("target")
            published = entry.get("published")
            proto = entry.get("protocol", "tcp")
            mode = entry.get("mode", "ingress")
        else:
            token = str(entry)
            proto = "tcp"
            if "/" in token:
                token, _, proto = token.rpartition("/")
            bits = token.split(":")
            if len(bits) == 1:
                published, target = None, bits[0]
            elif len(bits) == 2:
                published, target = bits[0], bits[1]
            else:  # ip:host:container
                published, target = bits[-2], bits[-1]
            mode = "ingress"

        if target is None:
            warnings.append(f"{service}: port entry '{entry}' without a target port — skipped.")
            continue
        if "-" in str(target) or (published and "-" in str(published)):
            warnings.append(
                f"{service}: port range '{entry}' is taken over unchanged — "
                f"the collision check does not apply to ranges."
            )
            result.append({"mode": mode, "target": str(target), "published": str(published or target),
                           "protocol": proto})
            continue

        try:
            target_i = int(str(target))
            preferred = int(str(published)) if published else target_i
        except ValueError:
            warnings.append(f"{service}: port entry '{entry}' is not numeric — skipped.")
            continue

        host_port = pick_host_port(preferred, used)
        if host_port != preferred:
            warnings.append(
                f"{service}: host port {preferred} is taken or ZimaOS-reserved → {host_port}."
            )
        result.append({"mode": mode, "target": target_i, "published": str(host_port), "protocol": proto})
    return result


# --- Volumes ----------------------------------------------------------------

def claim_dir(key, preferred, app_id, moved, warnings=None, service=None):
    """Reserve one directory under /DATA/AppData/<app>/ for `key`, exclusively.

    `moved` is shared across every volume of an app, and EVERY kind of source has
    to go through here — named volumes, relative paths, anonymous volumes and
    absolute paths alike. Two guarantees follow:
      - the same key always gets the same directory, so a volume used by two
        services stays one directory;
      - two different keys never get the same one. A named volume `db-data` and a
        bind `/opt/foo/db-data` used to collide silently (found by review
        2026-07-21); with a database on one side that corrupts on first start.
    A key is (kind, source) — `("volume", "db-data")` is deliberately not the same
    thing as `("abs", "/opt/foo/db-data")`.
    """
    if key in moved:
        return moved[key]
    stem = slugify(preferred) or "data"
    candidate = f"/DATA/AppData/{app_id}/{stem}"
    if candidate in moved.values():
        taken_by = next(k for k, v in moved.items() if v == candidate)
        suffix = 2
        while f"{candidate}-{suffix}" in moved.values():
            suffix += 1
        candidate = f"{candidate}-{suffix}"
        if warnings is not None:
            # Renaming data directories behind someone's back is exactly the kind
            # of thing that has to be said out loud.
            warnings.append(
                f"{service}: '{key[1]}' would land in /DATA/AppData/{app_id}/{stem}, "
                f"which is already taken by '{taken_by[1]}' — using {candidate} instead "
                f"so two unrelated sources do not share one directory."
            )
    moved[key] = candidate
    return candidate


def normalize_volumes(entries, app_id, named_volumes, warnings, service, moved=None):
    """Rewrite everything that is meant to be persistent to /DATA/AppData/<app>/ (Rule 7).

    Named volumes, relative paths, anonymous volumes and absolute paths outside
    /DATA get a fixed bind under /DATA/AppData; sockets/devices and real host
    locations (/media, /mnt) are left untouched.
    """
    result = []
    moved = {} if moved is None else moved
    for entry in entries or []:
        if isinstance(entry, dict):
            source, target = entry.get("source"), entry.get("target")
            read_only = bool(entry.get("read_only"))
            vtype = entry.get("type", "bind")
            if vtype == "volume" and source:
                # Named volumes become binds — the top-level volumes block is
                # dropped from the output, an undeclared volume would otherwise
                # be a compose error on the host.
                source = claim_dir(("volume", source), source, app_id, moved,
                                   warnings, service)
                vtype = "bind"
            if vtype != "bind":
                result.append(entry)                    # tmpfs/npipe unchanged
                continue
        else:
            bits = str(entry).split(":")
            read_only = False
            if len(bits) >= 2 and bits[-1] in ("ro", "rw", "z", "Z", "cached", "delegated", "consistent"):
                read_only = bits.pop() == "ro"
            if len(bits) == 1:
                source, target = None, bits[0]          # anonymous volume
            else:
                source, target = ":".join(bits[:-1]), bits[-1]
            vtype = "bind"

        if not target:
            warnings.append(f"{service}: volume entry '{entry}' without a target — skipped.")
            continue

        if source is None:
            source = claim_dir(("anon", target), target.strip("/").replace("/", "-"),
                               app_id, moved, warnings, service)
        elif source.startswith(TMPFS_PREFIXES) and not source.endswith(".sock"):
            # Stays where it is (it is in PASSTHROUGH_PREFIXES), but this one has to
            # be said out loud — the check therefore comes BEFORE the silent
            # passthrough branch below:
            # measured 2026-07-21, wings died after every reboot with exit 127 and
            # "failed to fulfil mount request: open /run/wings: no such file or
            # directory" — even with restart: always.
            warnings.append(
                f"{service}: bind mount {source} lies on a tmpfs (/run, /tmp) and stays "
                f"unchanged — but it is empty after a reboot, and docker only creates a "
                f"missing bind source when the container is CREATED, not when an existing "
                f"one is STARTED. The container then dies at every boot with exit 127 "
                f"('failed to fulfil mount request'), restart: always included. Either "
                f"have something recreate the directory at boot, or move it under "
                f"/DATA/AppData/{app_id}/ and point the app's config at the new path."
            )
        elif source.startswith(PASSTHROUGH_PREFIXES):
            pass                                        # socket/device stays where it is
        elif source.startswith("/DATA/"):
            pass                                        # already at the right anchor
        elif source.startswith("./") or source.startswith("../") or source.startswith("~"):
            stem = source.lstrip("./~").replace("/", "-") or target.strip("/").replace("/", "-")
            source = claim_dir(("rel", source), stem, app_id, moved, warnings, service)
        elif source.startswith(HOST_PATH_PREFIXES):
            warnings.append(
                f"{service}: bind mount {source} points at a real host location and stays "
                f"unchanged — make sure it exists (Rule 7)."
            )
        elif source.startswith("/"):
            new = claim_dir(("abs", source), source.rstrip("/").rsplit("/", 1)[-1],
                            app_id, moved, warnings, service)
            warnings.append(
                f"{service}: bind mount {source} → {new}. On ZimaOS only /DATA is app "
                f"storage. Parts of the root filesystem are mounted read-only — there "
                f"docker fails with 'mkdir …: read-only file system' and the container "
                f"never starts; what is writable (/etc, /opt, /var/lib) lies outside the "
                f"app data area and is not persistence-safe (Rule 7). If {source} already "
                f"holds data, copy it over by hand."
            )
            source = new
        elif source in named_volumes or re.match(r"^[A-Za-z0-9._-]+$", source):
            source = claim_dir(("volume", source), source, app_id, moved, warnings, service)
        else:
            warnings.append(f"{service}: volume source '{source}' cannot be classified — taken over unchanged.")

        item = {"type": "bind", "source": source, "target": target}
        if read_only:
            item["read_only"] = True
        result.append(item)
    return result


# --- Service selection ------------------------------------------------------

def pick_main_service(services, app_id, hint=None):
    """Find the service that provides the WebUI — x-casaos.main points at it.

    Order: explicit wish → name match with the app → first
    non-infrastructure service with ports → first service with ports.
    """
    if hint:
        if hint not in services:
            raise ConvertError(f"Service '{hint}' does not exist. Available: {', '.join(services)}")
        return hint

    with_ports = [n for n, s in services.items() if s.get("ports")]
    candidates = [n for n in with_ports if image_basename((services[n] or {}).get("image")) not in SUPPORTING_IMAGES]

    for name in (candidates or with_ports):
        if slugify(name) == app_id:
            return name
    if candidates:
        return candidates[0]
    if with_ports:
        return with_ports[0]
    raise ConvertError(
        "No service publishes a port — ZimaOS needs a WebUI port "
        "for the tile. Add a port in the source compose or pick the service explicitly."
    )


def analyze(doc):
    """Overview of a compose stack, the way the UI displays it."""
    services = doc.get("services") or {}
    rows = []
    for name, svc in services.items():
        svc = svc or {}
        rows.append({
            "name": name,
            "image": svc.get("image"),
            "build": bool(svc.get("build")),
            "ports": [str(p) if not isinstance(p, dict) else f"{p.get('published', '?')}:{p.get('target')}"
                      for p in (svc.get("ports") or [])],
            "volumes": len(svc.get("volumes") or []),
            "role": "support" if image_basename(svc.get("image")) in SUPPORTING_IMAGES else "app",
            "depends_on": list(svc.get("depends_on") or []),
        })
    return rows


# --- Conversion -------------------------------------------------------------

# Service keys that ZimaOS/compose accepts and that we pass through unchanged.
# Everything else is deliberately dropped (see DROPPED_KEYS).
KEPT_KEYS = [
    "image", "container_name", "command", "entrypoint", "user", "working_dir",
    "environment", "env_file", "depends_on", "healthcheck", "restart",
    "cap_add", "cap_drop", "security_opt", "devices", "group_add", "privileged",
    "shm_size", "sysctls", "ulimits", "tmpfs", "extra_hosts", "dns", "hostname",
    "stop_grace_period", "stop_signal", "init", "ipc", "pid", "runtime",
    "logging", "profiles", "labels",
]
DROPPED_KEYS = {"build", "networks", "ports", "volumes", "deploy", "network_mode", "links", "expose"}


def convert(doc, meta, options=None):
    """Compose document → ZimaOS-compliant compose document (multi-service capable).

    meta:    dict with name/title/author/category/tagline/description/icon/index/
             memory/cpus/main
    options: dict with used_ports (set) — host ports taken on the target system
    """
    options = options or {}
    services_in = doc.get("services") or {}
    if not services_in:
        raise ConvertError("The compose file contains no services.")

    warnings = []
    used = set(options.get("used_ports") or set())
    named_volumes = set((doc.get("volumes") or {}).keys())

    # First determine the WebUI service, then derive the app name from it:
    # the first service in the file is often the database or the broker, and
    # after that the directory under /DATA/AppData would be named wrongly.
    hint = meta.get("name") or meta.get("title")
    main = pick_main_service(services_in, slugify(hint) if hint else "", meta.get("main"))
    app_id = slugify(hint or app_name_from_image((services_in[main] or {}).get("image"), main))
    title = meta.get("title") or app_id
    network = f"{app_id}-network"

    services_out = {}
    main_ports = []
    # Shared across all services: the same host path has to end up in the same
    # directory (services sharing a folder keep sharing it), different ones never
    # in the same directory.
    moved_paths = {}
    for name, svc in services_in.items():
        svc = svc or {}
        if svc.get("build") and not svc.get("image"):
            raise ConvertError(
                f"Service '{name}' only has 'build:' and no 'image:'. ZimaOS installs "
                f"exclusively prebuilt images (§4.4.1) — build and push the image, then "
                f"reference it in the compose file."
            )
        if svc.get("build"):
            warnings.append(f"{name}: 'build:' removed, image: {svc['image']} remains (ZimaOS does not build).")

        out = {"image": svc.get("image")}
        for key in KEPT_KEYS:
            if key != "image" and key in svc:
                out[key] = svc[key]

        for key in svc:
            if key not in KEPT_KEYS and key not in DROPPED_KEYS:
                out[key] = svc[key]

        ports = normalize_ports(svc.get("ports"), used, warnings, name)
        if ports:
            out["ports"] = ports
        if name == main:
            main_ports = ports

        volumes = normalize_volumes(svc.get("volumes"), app_id, named_volumes, warnings,
                                    name, moved_paths)
        if volumes:
            out["volumes"] = volumes

        env = out.get("environment")
        if isinstance(env, dict):
            env = [f"{k}={'' if v is None else v}" for k, v in env.items()]
        env = list(env or [])

        # env_file points at a file that does not exist on the ZimaOS host —
        # an app there is EXACTLY ONE compose file. Insert the values instead of
        # dragging the reference along; without values this is a blocker, not a detail.
        if out.pop("env_file", None) is not None:
            env_defaults = options.get("env_defaults") or {}
            if env_defaults:
                have = {str(e).split("=", 1)[0] for e in env}
                env += [f"{k}={v}" for k, v in env_defaults.items() if k not in have]
                warnings.append(
                    f"{name}: env_file resolved from {options.get('env_source')} — "
                    f"{len(env_defaults)} values taken directly into environment. Please review them."
                )
            else:
                found = options.get("env_source")
                warnings.append(
                    f"{name}: 'env_file' removed — ZimaOS installs only a single "
                    f"compose file and does not create a .env. " + (
                        f"{found} was found, but contains only comments. "
                        if found else "There was also no .env to be found next to the source. ") +
                    f"Missing values have to be set as environment, otherwise the "
                    f"service starts up misconfigured."
                )

        if not any(str(e).startswith("TZ=") for e in env):
            env.append(f"TZ={meta.get('timezone', 'Europe/Berlin')}")
        out["environment"] = env

        out.setdefault("restart", "unless-stopped")
        out["networks"] = [network]

        labels = out.get("labels")
        if name == main and meta.get("icon"):
            if isinstance(labels, dict):
                labels.setdefault("icon", meta["icon"])
            else:
                out["labels"] = {"icon": meta["icon"]}

        if name == main:
            # Rule 6: the decimal separator MUST be a period — the ZimaOS-own UI
            # generates '14,92GB' in a German locale and fails on that itself.
            out["deploy"] = {"resources": {"limits": {
                "memory": str(meta.get("memory") or "2GB").replace(",", "."),
                "cpus": str(meta.get("cpus") or "2.00").replace(",", "."),
            }}}
            out["x-casaos"] = {
                "ports": [
                    {"container": str(p["target"]), "description": {
                        "en_us": f"WebUI port ({p['target']})" if i == 0 else f"Service port {p['target']}"}}
                    for i, p in enumerate(ports)
                ],
                "volumes": [
                    {"container": v["target"], "description": {"en_us": f"Data at {v['target']}"}}
                    for v in volumes
                    if v.get("type") == "bind" and str(v.get("source", "")).startswith("/DATA/")
                ],
            }

        services_out[name] = {k: v for k, v in out.items() if v is not None}

    if not main_ports:
        raise ConvertError(
            f"The WebUI service '{main}' publishes no port — without a host port "
            f"there is no port_map and therefore no clickable tile."
        )

    web_port = str(main_ports[0]["published"])

    result = {
        "name": app_id,
        "services": services_out,
        "networks": {network: {"driver": "bridge"}},
        "x-casaos": {
            "architectures": meta.get("architectures") or ["amd64", "arm64"],
            "main": main,
            "author": meta.get("author") or "",
            "developer": meta.get("developer") or meta.get("author") or "",
            "category": meta.get("category", "Utilities"),
            "icon": meta.get("icon", ""),
            "title": {"en_us": title, "custom": title},
            "tagline": {"en_us": meta.get("tagline") or "Self-hosted app"},
            "description": {"en_us": meta.get("description") or meta.get("tagline") or "Self-hosted app"},
            "index": meta.get("index") or "/",
            "is_uncontrolled": False,
            # Rule 5: port_map as a string, otherwise the tile vanishes wordlessly.
            "port_map": web_port,
        },
    }
    return result, {"main": main, "web_port": web_port, "warnings": warnings, "app_id": app_id}


def dump(doc):
    """Write YAML — preserve the order, no line breaks inside values."""
    return yaml.dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False, width=10000)


# --- Validation -------------------------------------------------------------

def _published_ports(svc):
    """The host ports a service publishes, as strings."""
    out = set()
    for p in (svc or {}).get("ports") or []:
        if isinstance(p, dict):
            if p.get("published") is not None:
                out.add(str(p["published"]))
        else:
            parts = str(p).split("/")[0].split(":")
            if len(parts) >= 2:
                out.add(parts[-2])
    return out


def _url_port(value):
    """The port an URL names, or None. '' when it names a host but no port."""
    m = re.match(r"^\s*(https?)://([^/\s]+)", str(value or ""))
    if not m:
        return None
    scheme, authority = m.group(1), m.group(2)
    host, _, port = authority.rpartition(":")
    if host and port.isdigit():
        return port
    return "443" if scheme == "https" else "80"


def framework_checks(services):
    """Traps that leave an app running, reachable — and unusable.

    Returns (problems, warnings). Each message names the value it read, so
    nobody has to trust the check: they can look at the same line.
    """
    problems, warnings = [], []
    for name, svc in (services or {}).items():
        svc = svc or {}
        env = _env_dict(svc.get("environment"))
        published = _published_ports(svc)

        for key, value in sorted(env.items()):
            lower = key.lower()
            text = "" if value is None else str(value).strip()

            # 1. The app is told its own address, and that address names a port
            #    it is not reachable on. Django-style origin checks then reject
            #    the login POST while everything else looks healthy (§4.4.2).
            if any(h in lower for h in PUBLIC_URL_HINTS) and text.startswith(("http://", "https://")):
                port = _url_port(text)
                if published and port and port not in published:
                    warnings.append(
                        f"{name}: {key}={text} names port {port}, but the service is published "
                        f"on {', '.join(sorted(published))}. An app that checks its own origin "
                        f"(Django, Rails and friends) accepts the page and rejects the login "
                        f"POST — it looks healthy from outside (§4.4.2). Correct as it stands "
                        f"only if a reverse proxy answers on {port} and forwards here."
                    )

            # 2. A secret that is empty or is the value everybody knows.
            if any(h in lower for h in SECRET_HINTS):
                if text == "":
                    # Deliberately a warning, not a problem: whether an empty
                    # secret is fatal cannot be read off the file. Measured on
                    # the live pterodactyl install, where MAIL_PASSWORD is empty
                    # on purpose and everything works. Calling that an error
                    # would teach people to ignore this class of message.
                    warnings.append(
                        f"{name}: {key} is empty. If the app needs it, it fails at a point where "
                        f"nothing reports an error — the container runs and the port answers. If "
                        f"it belongs to an optional feature (mail, S3, OIDC), empty is correct."
                    )
                elif text.lower() in PLACEHOLDER_VALUES:
                    problems.append(
                        f"{name}: {key}={text} is a well-known placeholder, not a secret. "
                        f"Anyone who knows the upstream compose knows this value."
                    )

        # 3. Half an admin account: the app comes up and nobody gets in, and
        #    nothing anywhere reports a problem.
        user_keys = [k for k in env if any(h in k.lower() for h in ADMIN_USER_HINTS)]
        password_keys = [k for k in env if any(h in k.lower() for h in ADMIN_PASSWORD_HINTS)]
        has_user = any(str(env[k] or "").strip() for k in user_keys)
        has_password = any(str(env[k] or "").strip() for k in password_keys)
        if user_keys and has_user and not has_password:
            warnings.append(
                f"{name}: {', '.join(sorted(user_keys))} is set, but no admin password "
                f"({' / '.join(sorted(password_keys)) or 'no such variable at all'}). Apps that "
                f"create their superuser on first start then create none — the app runs and "
                f"nobody can log in."
            )
        elif password_keys and has_password and not has_user:
            warnings.append(
                f"{name}: {', '.join(sorted(password_keys))} is set, but no admin user name. "
                f"Whether an account is created at all depends on the app."
            )
    return problems, warnings


def validate(text):
    """Checks exactly those traps that ZimaOS acknowledges silently or cryptically.

    Works structurally on the parsed YAML; if it is unparsable, that is exactly
    the first error (instead of a regex substitute check that waves wrong things through).
    """
    problems, warnings = [], []

    unresolved = sorted({m.group(1) for m in VAR_RE.finditer(text)})
    if unresolved:
        problems.append(
            "Unresolved variables: " + ", ".join("${%s}" % v for v in unresolved) +
            " — ZimaOS does not ship a .env, the stack starts up faulty with these."
        )

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"YAML is not parsable: {e}"], warnings
    if not isinstance(doc, dict):
        return ["YAML contains no mapping at the top level."], warnings

    if not str(doc.get("name") or "").strip():
        problems.append("Top-level 'name:' is missing — ZimaOS needs it as the app ID (Rule 4).")

    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        problems.append("No 'services:' block — nothing installable comes out of that.")
        services = {}

    casaos = doc.get("x-casaos")
    if not isinstance(casaos, dict):
        problems.append("The x-casaos block is missing — without it no tile appears.")
        casaos = {}

    main = casaos.get("main")
    if not main:
        problems.append("x-casaos.main is missing — it points at the WebUI service.")
    elif main not in services:
        problems.append(f"x-casaos.main '{main}' is not a service in the stack ({', '.join(services)}).")

    port_map = casaos.get("port_map")
    if port_map is None:
        problems.append("x-casaos.port_map is missing — without a WebUI port the tile stays dead.")
    elif not isinstance(port_map, str):
        problems.append(
            f"port_map is not a string ({port_map!r}) — an int breaks the parser and the app "
            f"vanishes wordlessly from the grid (Rule 5)."
        )
    elif main in services:
        published = set()
        for p in (services[main] or {}).get("ports") or []:
            published.add(str(p.get("published")) if isinstance(p, dict) else str(p).split(":")[0])
        if published and port_map not in published:
            problems.append(
                f"port_map '{port_map}' is not a published port of '{main}' ({', '.join(sorted(published))})."
            )

    if not casaos.get("icon"):
        warnings.append("No icon set — the tile stays empty (Rule 8).")
    if casaos.get("category") and casaos["category"] not in VALID_CATEGORIES:
        warnings.append(f"Category '{casaos['category']}' is not a ZimaOS standard category.")
    if not (casaos.get("title") or {}).get("en_us"):
        problems.append("x-casaos.title.en_us is missing — mandatory field (§5.1).")
    if not (casaos.get("description") or {}).get("en_us"):
        warnings.append("x-casaos.description.en_us is missing — mandatory field according to §5.1.")

    all_published = {}
    for name, svc in services.items():
        svc = svc or {}
        if svc.get("build"):
            problems.append(f"Service '{name}' has 'build:' — ZimaOS does not build images (§4.4.1).")
        if not svc.get("image"):
            problems.append(f"Service '{name}' has no 'image:'.")
        if svc.get("env_file"):
            problems.append(
                f"Service '{name}' refers to 'env_file' — a ZimaOS app is exactly ONE "
                f"compose file, the referenced file does not exist on the host. "
                f"Move the values into 'environment:'."
            )

        for p in svc.get("ports") or []:
            if isinstance(p, dict):
                pub = p.get("published")
                if pub is not None and not isinstance(pub, str):
                    problems.append(f"{name}: published {pub!r} is not a string — Rule 5 applies here too.")
                key = str(pub)
            else:
                key = str(p).split(":")[0]
            if key.isdigit():
                if int(key) in RESERVED_PORTS:
                    problems.append(f"{name}: host port {key} is taken by ZimaOS (§8.3/ZFW whitelist).")
                if key in all_published:
                    problems.append(f"Host port {key} is taken by both '{all_published[key]}' and '{name}'.")
                all_published[key] = name

        for v in svc.get("volumes") or []:
            source = v.get("source") if isinstance(v, dict) else str(v).split(":")[0]
            if not source:
                continue
            if source.startswith("./") or source.startswith("../"):
                problems.append(
                    f"{name}: relative bind '{source}' — on the ZimaOS host the "
                    f"source directory does not exist (§4.4.2)."
                )
            elif source.startswith("/") and not source.startswith("/DATA/") \
                    and not source.startswith(PASSTHROUGH_PREFIXES):
                warnings.append(f"{name}: bind mount {source} lies outside /DATA — not persistence-safe (Rule 7).")
            if re.match(r"^/media/sd[a-z]", str(source)):
                warnings.append(f"{name}: {source} uses a drive letter — breaks after a reboot. Use a UUID path.")

        limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        for field in ("memory", "cpus"):
            value = str(limits.get(field, ""))
            if "," in value:
                problems.append(
                    f"{name}: {field} '{value}' uses a comma — ZimaOS parses with "
                    f"strconv.ParseFloat and answers HTTP 400. Use a period (Rule 6)."
                )

    declared = set(services)
    for name, svc in services.items():
        for dep in ((svc or {}).get("depends_on") or []):
            if dep not in declared:
                problems.append(f"{name}: depends_on '{dep}' does not exist in the stack.")

    # Traps that ZimaOS cannot see at all, because the container is running and
    # the port answers — they only show up when someone tries to log in.
    framework_problems, framework_warnings = framework_checks(services)
    problems += framework_problems
    warnings += framework_warnings

    return problems, warnings


# --- ZimaOS API -------------------------------------------------------------

def api(host, method, path, token=None, body=None, content_type="application/json", timeout=60):
    req = urllib.request.Request(f"http://{host}{path}", method=method)
    if token:
        req.add_header("Authorization", token)  # Rule 1: without "Bearer "
    if body is not None:
        data = body.encode() if isinstance(body, str) else body
        req.add_header("Content-Type", content_type)
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except urllib.error.URLError as e:
        raise ConvertError(f"{host} not reachable: {e.reason}") from e


def login(host, user, password):
    status, raw = api(host, "POST", LOGIN_PATH, body=json.dumps({"username": user, "password": password}))
    if status != 200:
        raise ConvertError(f"Login on {host} failed (HTTP {status}): {raw[:200]}")
    try:
        return json.loads(raw)["data"]["token"]["access_token"]
    except (KeyError, json.JSONDecodeError) as e:
        raise ConvertError(f"Unexpected login response from {host}: {raw[:200]}") from e


def install(host, user, password, text):
    token = login(host, user, password)
    status, raw = api(host, "POST", INSTALL_PATH, token, text, "application/yaml")
    return status, raw


def uninstall(host, user, password, name):
    token = login(host, user, password)
    return api(host, "DELETE", f"{INSTALL_PATH}/{name}?delete_config_folder=true", token)


# --- One call from URL to YAML ---------------------------------------------

def build_from_source(url_or_path, meta, variables=None, options=None):
    """The complete path: fetch URL → detect kind → resolve variables → convert.

    Returns (yaml_text, info). info contains main, web_port, warnings,
    generated (generated secrets) and the validation results.
    """
    options = options or {}
    if options.get("source_text"):
        # A file opened in the browser: the content arrives with the request. It
        # has no directory to look next to, which matters for env_file below.
        text = options["source_text"]
        effective = url_or_path or options.get("source_name") or "(opened file)"
        if not text.strip():
            raise ConvertError("The opened file is empty.")
    else:
        text, effective = fetch_source(url_or_path)
    kind = detect_kind(text)

    # Resolve env_file references BEFORE variables are replaced: the .env next to
    # the source is usually exactly the place where ${UPLOAD_LOCATION} & co. are
    # defined. User values beat them.
    env_values, env_source = {}, None
    extra_notes = []
    names = env_file_names(text)
    if names:
        env_values, env_source = fetch_env_files(effective, names)
        if options.get("source_text") and not env_values:
            # Worth saying explicitly: with a URL we can look next to the source,
            # with an opened file there is no directory to look in at all. The
            # generic "no .env found" would suggest we searched and came up empty.
            extra_notes.append(
                f"The file refers to an env_file ({', '.join(names)}), and an opened "
                f"file has no directory to look next to — nothing was searched. ZimaOS "
                f"creates no .env either, so those values have to be set by hand below."
            )

    merged = dict(env_values)
    merged.update(variables or {})
    resolved, generated = resolve_variables(
        text, merged, autofill_secrets=options.get("autofill_secrets", True)
    )

    # What goes into 'environment' has to be what was substituted into the text —
    # otherwise one file gives two answers. Setting IMMICH_VERSION=v2.1.0 used to
    # produce 'image: …:v2.1.0' next to 'IMMICH_VERSION=v3' from example.env.
    # The key set stays the env file's (a user value for a variable that is not in
    # it is a substitution, not a new environment entry); only the value follows
    # the user. `or v` mirrors resolve_variables, which also ignores empty values.
    user_values = variables or {}
    env_effective = {k: (user_values.get(k) or v) for k, v in env_values.items()}
    options = dict(options, env_defaults=env_effective, env_source=env_source)

    if kind == "dockerfile":
        image = (meta.get("image") or "").strip()
        if not image:
            raise ConvertError(
                "This is a Dockerfile. ZimaOS only installs prebuilt images — please state the "
                "image name (e.g. ghcr.io/dir/app:1.2) under which the built image is "
                "reachable. The ports/volumes/ENV from the Dockerfile are taken over."
            )
        df = parse_dockerfile(resolved)
        if not df["ports"]:
            # A Dockerfile without EXPOSE says nothing about the WebUI port —
            # guessing would be exactly the mistake that ZimaOS acknowledges with
            # a dead tile.
            if not meta.get("port"):
                raise ConvertError(
                    "The Dockerfile contains no EXPOSE — the WebUI port cannot be derived "
                    "from it. Please state the container port (CLI: --port, UI: field "
                    "'WebUI port')."
                )
            df["ports"] = [int(meta["port"])]
        doc = compose_from_dockerfile(df, image)
    else:
        try:
            doc = yaml.safe_load(resolved)
        except yaml.YAMLError as e:
            raise ConvertError(f"The compose YAML is not parsable: {e}") from e

    result, info = convert(doc, meta, options)
    yaml_text = dump(result)
    problems, warnings = validate(yaml_text)

    if options.get("check_icon", True):
        reason = check_icon(meta.get("icon"))
        if reason:
            warnings.append(
                f"Icon {meta.get('icon') or '(empty)'} is not retrievable ({reason}) — "
                f"the tile stays empty, ZimaOS does not report that (Rule 8)."
            )

    info.update({
        "kind": kind, "source": effective, "generated": generated,
        "problems": problems, "warnings": extra_notes + info["warnings"] + warnings,
    })
    return yaml_text, info


# --- Blueprints -------------------------------------------------------------
#
# A blueprint is deliberately NOT a stored compose file. It holds only what
# cannot be derived from upstream: which URL to start from, the values an app
# needs to actually be usable (admin user, secret key, its own external URL),
# and — the part that makes "tested" mean anything — the expectations that get
# executed against the running installation.
#
# Storing the converted compose instead would rot silently: upstream changes
# image tags, volume paths and env names, and months later the catalogue hands
# out a "tested" file that no longer matches reality.

BLUEPRINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blueprints")

# Where blueprints saved from the UI go. The one baked into the image is read-only
# at runtime (and gone at the next recreate — the app's container has no volume of
# its own), so saving needs a directory that outlives the container. Unset means
# the feature is simply off, and saving says so instead of writing into the void.
USER_BLUEPRINT_DIR = os.environ.get("ZIMAPP_BLUEPRINT_DIR") or None

# Placeholders inside blueprint env values. Resolved only AFTER conversion,
# because ${port} is not known before the port assignment has run.
PLACEHOLDER_RE = re.compile(r"\$\{(generate:(\d+)|host|port|app|scheme)\}")


def blueprint_dirs():
    """Where blueprints are read from: shipped ones first, saved ones second."""
    dirs = [BLUEPRINT_DIR]
    if USER_BLUEPRINT_DIR and os.path.abspath(USER_BLUEPRINT_DIR) != os.path.abspath(BLUEPRINT_DIR):
        dirs.append(USER_BLUEPRINT_DIR)
    return dirs


def blueprint_path(name, allow_paths=False):
    """Resolve a blueprint NAME to a file inside the blueprint directories.

    A name that arrives over HTTP must never be usable as a path: the server
    binds to 0.0.0.0 without authentication, so a caller-supplied path would open
    arbitrary files — and the YAML parse error quotes their content back. Only the
    local CLI, running in the user's own shell, may pass a real path.
    """
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name) \
            or name.endswith((".yml", ".yaml")):
        if allow_paths:
            return name
        raise ConvertError(
            f"'{name}' is a name, not a path — a blueprint is addressed by its plain "
            f"name (e.g. 'paperless-ngx'). Paths are only accepted on the command line."
        )
    for directory in blueprint_dirs():
        candidate = os.path.join(directory, f"{name}.yml")
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(BLUEPRINT_DIR, f"{name}.yml")     # for the "not found" message


def list_blueprints():
    """All blueprints with the fields the UI and the CLI listing need."""
    out, seen = [], set()
    for directory in blueprint_dirs():
        if not os.path.isdir(directory):
            continue                        # e.g. an encrypted folder that is still locked
        saved = os.path.abspath(directory) != os.path.abspath(BLUEPRINT_DIR)
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith((".yml", ".yaml")):
                continue
            try:
                bp = load_blueprint(os.path.join(directory, entry), allow_paths=True)
            except ConvertError:
                continue                    # a broken file must not kill the listing
            if bp["name"] in seen:
                continue                    # a shipped blueprint is never shadowed
            seen.add(bp["name"])
            # YAML turns an unquoted 2026-07-19 into a datetime.date, which json
            # cannot serialise — that took down /api/defaults entirely. Normalise
            # to strings here, where the data leaves the YAML world.
            verified = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in (bp.get("verified") or {}).items()}
            row = {
                "name": bp["name"],
                "title": bp.get("title") or bp["name"],
                "category": bp.get("category", "Utilities"),
                "tagline": bp.get("tagline", ""),
                "source": bp["source"],
                "verified": verified or None,
                "expectations": len(bp.get("expect") or []),
                "saved": saved,             # shipped recipe vs. one saved here
            }
            # The form fills itself from these when a blueprint is picked, so the
            # recipe's values are visible and editable instead of being silently
            # overridden by the form's own defaults (review 2026-07-21).
            # `vars` deliberately stays out: this listing is served unauthenticated
            # and a saved blueprint may hold a password in cleartext.
            for key in ("description", "icon", "memory", "cpus", "index",
                        "main", "author", "image", "port"):
                if bp.get(key):
                    row[key] = bp[key]
            out.append(row)
    return out


BLUEPRINT_HEADER = """\
# Saved by zimapp on {date} from the form — NOT a verified recipe.
#
# A blueprint is a delta, not a copy of the compose: it keeps the source URL and
# the values an app needs to be usable. That way it does not rot silently when
# upstream changes an image tag or an env name — but it also means the result is
# only as good as the source it points at.
#
# What is deliberately missing: a `verified:` block. That field records what was
# actually observed on a running installation; nothing has been observed here, so
# claiming it would be a lie. Run `zimapp verify {name}` after installing, add
# `expect:` assertions, and write down what really held.
"""


def _today():
    return time.strftime("%Y-%m-%d")


def secret_looking(names):
    """Which of these variable names look like secrets (same rule as autofill)."""
    return sorted(n for n in names if any(h in n.lower() for h in SECRET_HINTS))


def save_blueprint(data, overwrite=False):
    """Write a blueprint from the form into the writable blueprint directory.

    Returns (path, warnings). Everything that can go wrong is raised or reported,
    never swallowed: a save that silently did not happen is worse than an error.
    """
    if not USER_BLUEPRINT_DIR:
        raise ConvertError(
            "Saving is switched off: no writable blueprint directory is configured. "
            "Set ZIMAPP_BLUEPRINT_DIR and mount that path into the container — the "
            "directory baked into the image is read-only and gone at the next recreate."
        )
    if not os.path.isdir(USER_BLUEPRINT_DIR):
        raise ConvertError(
            f"The blueprint directory {USER_BLUEPRINT_DIR} does not exist right now. "
            f"If it is an encrypted folder, it is probably still locked — unlock it in "
            f"ZimaOS and save again. Nothing was written."
        )

    name = slugify(str(data.get("name") or "").strip())
    if not name:
        raise ConvertError("A blueprint needs a name (the app id).")
    source = str(data.get("source") or "").strip()
    if not source:
        raise ConvertError(
            "A blueprint stores the source URL, not the generated compose — so a "
            "source is mandatory. Blueprints built from a local file cannot be saved."
        )

    shipped = os.path.join(BLUEPRINT_DIR, f"{name}.yml")
    if os.path.isfile(shipped):
        raise ConvertError(
            f"'{name}' is a blueprint that ships with zimapp. Pick another name — "
            f"shadowing it would make it unclear which recipe is being used."
        )
    path = os.path.join(USER_BLUEPRINT_DIR, f"{name}.yml")
    if os.path.isfile(path) and not overwrite:
        raise ConvertError(f"{path} already exists. Save again with overwrite to replace it.")

    bp = {"name": name, "source": source}
    # author/image/port were dropped silently before (review 2026-07-21) — a
    # blueprint saved from a Dockerfile source could never be regenerated, while
    # the UI reported success.
    for key in ("title", "category", "tagline", "description", "icon", "author",
                "main", "memory", "cpus", "index", "image", "port"):
        value = (data.get(key) or "").strip() if isinstance(data.get(key), str) else data.get(key)
        if value:
            bp[key] = value
    variables = {k: v for k, v in (data.get("vars") or {}).items() if str(v).strip()}
    if variables:
        bp["vars"] = variables

    warnings = []
    secrets_in = secret_looking(variables)
    if secrets_in:
        warnings.append(
            "This blueprint contains values in cleartext that look like secrets: "
            + ", ".join(secrets_in) +
            f". The file is written with mode 0660 — owner and group can read it, "
            f"nobody else. Deliberately not 0600: the app writes under its own uid, so "
            f"0600 would lock YOU out of your own file. The directory is what really "
            f"protects it, so put {USER_BLUEPRINT_DIR} on an encrypted share if it "
            f"matters. Alternative: replace the value by ${{generate:24}} in the file — "
            f"then a fresh secret is generated at every install and nothing sensitive "
            f"is stored at all."
        )

    text = BLUEPRINT_HEADER.format(date=_today(), name=name) + "\n" + dump(bp)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o660)
        os.replace(tmp, path)               # atomic: no half-written blueprint
    except OSError as e:
        # A bare "Permission denied: /blueprints/x.yml.tmp" says nothing about what
        # to do. Inside a container the cause is almost always ownership: the app
        # runs under its own uid, the directory belongs to someone else.
        try:
            st = os.stat(USER_BLUEPRINT_DIR)
            owner = f"owner {st.st_uid}:{st.st_gid}, mode {oct(st.st_mode & 0o777)}"
        except OSError:
            owner = "not readable"
        raise ConvertError(
            f"Could not write to {USER_BLUEPRINT_DIR} ({e.strerror}). The directory is "
            f"{owner}; this process runs as {os.getuid()}:{os.getgid()}. Give that user "
            f"write access to the directory (on the host, for the mounted path). "
            f"Nothing was written."
        ) from e
    warnings.append(f"Saved as {path} (mode 0660). It is not a verified recipe — "
                    f"there is no 'verified:' block until something was actually proven.")
    return path, warnings


def load_blueprint(name, allow_paths=False):
    path = blueprint_path(name, allow_paths=allow_paths)
    if not os.path.isfile(path):
        available = ", ".join(b["name"] for b in list_blueprints()) or "(none)"
        raise ConvertError(f"Blueprint '{name}' not found at {path}. Available: {available}")
    with open(path, encoding="utf-8") as fh:
        try:
            bp = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            raise ConvertError(f"Blueprint {path} is not parsable: {e}") from e

    if not isinstance(bp, dict):
        raise ConvertError(f"Blueprint {path} does not contain a mapping.")
    for field in ("name", "source"):
        if not bp.get(field):
            raise ConvertError(f"Blueprint {path} is missing the mandatory field '{field}'.")
    if bp.get("env") and not isinstance(bp["env"], dict):
        raise ConvertError(f"Blueprint {path}: 'env' must be a mapping of service -> values.")
    bp["_path"] = path
    return bp


def blueprint_source_url(bp):
    """Apply the pin: a blueprint points at a commit, not at a moving branch.

    Without this the catalogue silently tracks upstream's main branch, and a
    blueprint that was verified once says nothing about what it fetches today.
    """
    url, pin = bp["source"], bp.get("pin")
    if not pin:
        return url, False
    pinned = re.sub(r"(raw\.githubusercontent\.com/[^/]+/[^/]+/)[^/]+/", rf"\g<1>{pin}/", url)
    return pinned, pinned != url


def expand_placeholders(value, context, generated):
    """${generate:N}, ${host}, ${port}, ${app}, ${scheme} in blueprint values."""
    def sub(match):
        token = match.group(1)
        if token.startswith("generate:"):
            key = f"{context.get('_field', 'secret')}"
            if key not in generated:
                generated[key] = secrets.token_urlsafe(int(match.group(2)))
            return generated[key]
        return str(context.get(token, match.group(0)))
    return PLACEHOLDER_RE.sub(sub, str(value))


def apply_blueprint_env(doc, bp, host, web_port, app_id):
    """Write the blueprint's env values into the converted compose.

    Runs after convert() on purpose: only then are the host port and the app id
    fixed, and values like PAPERLESS_URL need exactly those.

    Returns the secrets that were generated — they exist nowhere else, so
    swallowing them here would lock the user out of their own app.
    """
    generated = {}
    services = doc.get("services") or {}
    for service, values in (bp.get("env") or {}).items():
        if service not in services:
            raise ConvertError(
                f"Blueprint {bp['name']}: service '{service}' does not exist in the converted "
                f"compose ({', '.join(services)}) — upstream probably renamed it."
            )
        env = list((services[service] or {}).get("environment") or [])
        present = {str(e).split("=", 1)[0] for e in env}
        for key, raw in (values or {}).items():
            context = {"host": host or "", "port": web_port, "app": app_id,
                       "scheme": "http", "_field": key}
            value = expand_placeholders(raw, context, generated)
            if key in present:
                env = [f"{key}={value}" if str(e).split("=", 1)[0] == key else e for e in env]
            else:
                env.append(f"{key}={value}")
        services[service]["environment"] = env
    return generated


# --- Expectations: what makes "tested" checkable ----------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand the redirect back to the caller instead of following it.

    urllib raises HTTPError for these, which run_expectations already treats as
    a normal answer — so a 302 arrives as a 302.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def run_expectations(base_url, expectations, timeout=15):
    """Run a blueprint's expect block against a running installation.

    Deliberately asserts on the PAYLOAD, not just the status code: an app that
    answers 200 with an empty body or a raw i18n key is broken, and a status-only
    check would call it healthy.

    Returns a list of {expectation, ok, detail} — never raises for a failed
    expectation, because the caller wants the full picture, not the first miss.
    """
    results = []
    for exp in expectations or []:
        path = exp.get("http", "/")
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        entry = {"expectation": exp, "url": url, "ok": False, "detail": ""}
        req = urllib.request.Request(url, headers={"User-Agent": "zimapp-verify"})
        # Do NOT follow redirects by default: urlopen would silently turn a 302
        # into the 200 of the target page, so `status: 302` could never match and
        # a broken redirect chain would look healthy. Opt in with `follow: true`.
        opener = (urllib.request.build_opener() if exp.get("follow")
                  else urllib.request.build_opener(_NoRedirect))
        try:
            with opener.open(req, timeout=timeout) as resp:
                status, body = resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            entry["detail"] = f"not reachable: {e.reason}"
            results.append(entry)
            continue
        except OSError as e:
            entry["detail"] = f"not reachable: {e}"
            results.append(entry)
            continue

        problems = []
        want_status = exp.get("status")
        if want_status is not None and status != want_status:
            problems.append(f"status {status}, expected {want_status}")
        for needle in ([exp["contains"]] if isinstance(exp.get("contains"), str)
                       else exp.get("contains") or []):
            if needle not in body:
                problems.append(f"missing in body: {needle!r}")
        for needle in ([exp["absent"]] if isinstance(exp.get("absent"), str)
                       else exp.get("absent") or []):
            if needle in body:
                problems.append(f"must not appear in body: {needle!r}")
        min_bytes = exp.get("min_bytes")
        if min_bytes is not None and len(body) < min_bytes:
            problems.append(f"body only {len(body)} bytes, expected at least {min_bytes}")

        entry["ok"] = not problems
        entry["detail"] = f"HTTP {status}, {len(body)} bytes" if entry["ok"] else "; ".join(problems)
        results.append(entry)
    return results


def app_base_url(host, name, user, password):
    """Find a running app's URL via the app grid — no port guessing."""
    token = login(host, user, password)
    status, raw = api(host, "GET", "/v2/app_management/web/appgrid", token)
    if status != 200:
        raise ConvertError(f"App grid on {host} returned HTTP {status}: {raw[:200]}")
    for app in json.loads(raw).get("data") or []:
        if (app or {}).get("name") == name:
            port = str(app.get("port") or "").strip()
            if not port:
                raise ConvertError(f"App '{name}' is installed but reports no port.")
            scheme = app.get("scheme") or "http"
            return f"{scheme}://{host}:{port}", app.get("status")
    raise ConvertError(f"App '{name}' is not installed on {host}.")


# --- Update: read back, compare, apply --------------------------------------
#
# ZimaOS stores a normalised version of what it was sent, so a textual diff
# between "the file I have" and "what is installed" is nothing but noise. Every
# transformation below was read off the live system on 2026-07-21 by installing
# a known file and fetching it back:
#
#   environment:  ["A=b"]        -> {"A": "b"}
#   memory:       "1GB"          -> "1073741824"   (bytes, as a string)
#   networks:     {n: {driver}}  -> gains name/external/ipam
#   services:     command/entrypoint: null and deploy.resources.placement: {} added
#   services.*.x-casaos          -> NOT returned at all
#
# The last one is the reason `SERVICE_X_CASAOS_NOTE` exists: that block cannot be
# compared, and a comparison that quietly skips a part of the file is exactly the
# kind of assurance that looks green without having looked.

MEMORY_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2,
                "g": 1024**3, "gb": 1024**3, "t": 1024**4, "tb": 1024**4}

SERVICE_X_CASAOS_NOTE = (
    "the per-service 'x-casaos' block (port/volume descriptions) is not part of "
    "the comparison — the ZimaOS API does not return it; neither is "
    "x-casaos.store_app_id, which ZimaOS adds on installation"
)

# Fields inside the top-level x-casaos block that ZimaOS fills in itself. They
# are in every installed app and in none of ours, so comparing them would report
# a removal on every single update.
X_CASAOS_ADDED_BY_ZIMAOS = ("store_app_id",)


def _memory_bytes(value):
    """'1GB' / '512m' / '1073741824' -> int. Unparsable stays a string."""
    if value is None:
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)", text)
    if not m:
        return text
    number, unit = float(m.group(1)), m.group(2).lower()
    if unit and unit not in MEMORY_UNITS:
        return text
    return int(number * MEMORY_UNITS.get(unit, 1))


def _env_dict(value):
    """environment as list or dict -> dict. 'A' without '=' means 'take from the host'."""
    if isinstance(value, dict):
        return {str(k): (None if v is None else str(v)) for k, v in value.items()}
    out = {}
    for item in value or []:
        key, sep, val = str(item).partition("=")
        out[key] = val if sep else None
    return out


def _port_key(entry):
    """One published port as a comparable tuple, whatever notation it arrived in."""
    if isinstance(entry, dict):
        return (str(entry.get("target") or ""), str(entry.get("published") or ""),
                str(entry.get("protocol") or "tcp"))
    text = str(entry)
    protocol = "tcp"
    if "/" in text:
        text, protocol = text.rsplit("/", 1)
    parts = text.split(":")
    published, target = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", parts[-1])
    return (target, published, protocol)


def _volume_key(entry):
    if isinstance(entry, dict):
        return (str(entry.get("target") or ""), str(entry.get("source") or ""),
                str(entry.get("type") or "bind"))
    parts = str(entry).split(":")
    return (parts[1] if len(parts) > 1 else parts[0], parts[0], "bind")


def normalize_for_compare(doc):
    """A compose document reduced to what can honestly be compared.

    Everything ZimaOS adds or rewrites on its own is levelled here, so a diff
    shows changes that someone actually made — not the storage format.
    """
    doc = doc or {}
    x_casaos = {k: v for k, v in (doc.get("x-casaos") or {}).items()
                if k not in X_CASAOS_ADDED_BY_ZIMAOS}
    out = {"name": doc.get("name"), "services": {}, "x-casaos": x_casaos}

    for name, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        clean = {}
        for key, value in svc.items():
            if key in ("command", "entrypoint") and value is None:
                continue          # ZimaOS adds these as null
            if key == "x-casaos":
                continue          # not returned by the API — see SERVICE_X_CASAOS_NOTE
            if key == "environment":
                clean["environment"] = _env_dict(value)
            elif key == "ports":
                clean["ports"] = sorted(_port_key(p) for p in value or [])
            elif key == "volumes":
                clean["volumes"] = sorted(_volume_key(v) for v in value or [])
            elif key == "networks":
                clean["networks"] = sorted(value.keys() if isinstance(value, dict) else value or [])
            elif key == "depends_on":
                # Short list and long form mean the same thing; 'service_started'
                # is docker's default. Comparing them raw would report a change
                # on every update while nothing changed — but a real
                # 'service_healthy' still shows up.
                if isinstance(value, dict):
                    clean["depends_on"] = {k: ((v or {}).get("condition") or "service_started")
                                           for k, v in value.items()}
                else:
                    clean["depends_on"] = {str(k): "service_started" for k in value or []}
            elif key == "deploy":
                # Only memory and cpus need levelling (ZimaOS stores bytes and
                # adds an empty 'placement'). Everything else in deploy —
                # replicas, restart_policy, reservations — is kept as it is:
                # reducing this block to the two fields we happen to write would
                # make a change to any other one invisible, and a diff that
                # quietly skips part of the file is worse than no diff.
                deploy = json.loads(json.dumps(value or {}))     # never touch the caller's doc
                deploy.pop("placement", None)
                limits = (deploy.get("resources") or {}).get("limits") or {}
                if limits.get("memory") is not None:
                    limits["memory"] = _memory_bytes(limits["memory"])
                if limits.get("cpus") is not None:
                    try:
                        limits["cpus"] = float(str(limits["cpus"]).replace(",", "."))
                    except ValueError:
                        limits["cpus"] = str(limits["cpus"])
                clean["deploy"] = deploy
            else:
                clean[key] = value
        out["services"][name] = clean

    # Only the network names and their driver are ours; name/external/ipam are
    # filled in by ZimaOS. 'default' is added by compose itself.
    networks = {}
    for name, spec in (doc.get("networks") or {}).items():
        if name == "default":
            continue
        networks[name] = (spec or {}).get("driver")
    out["networks"] = networks
    return out


def _flatten(value, prefix=""):
    """Nested structure -> {'a.b.c': leaf}. Lists are compared as a whole."""
    if isinstance(value, dict):
        flat = {}
        for key, sub in value.items():
            flat.update(_flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def compose_diff(installed, desired):
    """What would change, field by field.

    Returns a list of {path, installed, desired, kind} with kind in
    add/remove/change. Both documents go through normalize_for_compare first.
    """
    left = _flatten(normalize_for_compare(installed))
    right = _flatten(normalize_for_compare(desired))
    changes = []
    for path in sorted(set(left) | set(right)):
        old, new = left.get(path, KeyError), right.get(path, KeyError)
        if old is KeyError:
            changes.append({"path": path, "installed": None, "desired": new, "kind": "add"})
        elif new is KeyError:
            changes.append({"path": path, "installed": old, "desired": None, "kind": "remove"})
        elif old != new:
            changes.append({"path": path, "installed": old, "desired": new, "kind": "change"})
    return changes


def installed_compose(host, name, user=None, password=None, token=None):
    """The compose ZimaOS actually stores for an installed app.

    Returns (doc, status, token). This is the only honest starting point for an
    update: the file on the machine that generated it may be older, newer, or
    from a different machine altogether.
    """
    token = token or login(host, user, password)
    status, raw = api(host, "GET", f"{INSTALL_PATH}/{name}", token)
    if status == 404:
        raise ConvertError(
            f"'{name}' is not installed on {host}. `update` changes an existing app — "
            f"use `install` for a new one."
        )
    if status != 200:
        raise ConvertError(f"Reading the installed compose of '{name}' failed (HTTP {status}): {raw[:200]}")
    try:
        data = json.loads(raw)["data"]
    except (KeyError, json.JSONDecodeError) as e:
        raise ConvertError(f"Unexpected answer for '{name}': {raw[:200]}") from e
    return data.get("compose") or {}, data.get("status"), token


def meta_from_installed(doc):
    """The x-casaos block of an installed app, back in the shape convert() wants.

    Without this, re-converting from the source would reset title, icon and
    category to whatever the generator guesses, and the diff would be full of
    changes nobody asked for.
    """
    xc = (doc or {}).get("x-casaos") or {}
    title = xc.get("title") or {}
    main = xc.get("main") or ""
    service = ((doc.get("services") or {}).get(main) or {}) if main else {}
    limits = ((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    meta = {
        "name": doc.get("name") or "",
        "title": (title.get("custom") or title.get("en_us") or "") if isinstance(title, dict) else str(title),
        "author": xc.get("author") or "",
        "category": xc.get("category") or "",
        "tagline": (xc.get("tagline") or {}).get("en_us", "") if isinstance(xc.get("tagline"), dict) else "",
        "description": (xc.get("description") or {}).get("en_us", "") if isinstance(xc.get("description"), dict) else "",
        "icon": xc.get("icon") or "",
        "index": xc.get("index") or "",
        "main": main,
        "memory": limits.get("memory") or "",
        "cpus": limits.get("cpus") or "",
    }
    return {k: v for k, v in meta.items() if v not in ("", None)}


def installed_env(doc):
    """{service: {KEY: value}} of an installed app — the values an update must keep.

    Re-converting from the source would generate fresh passwords, and a fresh
    POSTGRES_PASSWORD against an existing database volume means the app never
    comes up again. So the installed values win over anything generated, and the
    caller has to override deliberately.
    """
    return {name: _env_dict((svc or {}).get("environment"))
            for name, svc in ((doc or {}).get("services") or {}).items()}


def keep_installed_values(doc, installed, generated, force=()):
    """Put values that already run back into a freshly generated compose.

    A regenerated secret is not a new secret, it is a broken app: a fresh
    POSTGRES_PASSWORD against an existing data volume locks the database out of
    its own files, a fresh SECRET_KEY invalidates every session. Only values
    that were GENERATED are restored this way — whatever the source or a
    blueprint states explicitly is a deliberate change and stays visible in the
    diff, where someone can decide about it.

    `force` names further keys to keep regardless of where the new value comes
    from — for the case where a blueprint states a value that is right for a
    fresh installation but wrong for this one (an admin user, say).

    Returns the names that were put back. They are no longer generated, and the
    caller must stop presenting them as such.
    """
    force = set(force or ())
    if not generated and not force:
        return []
    existing = installed_env(installed)
    kept = []
    for service, svc in (doc.get("services") or {}).items():
        entries = (svc or {}).get("environment")
        if not entries:
            continue
        have = existing.get(service) or {}
        rebuilt = []
        for entry in entries if isinstance(entries, list) else [f"{k}={v}" for k, v in entries.items()]:
            key, sep, _ = str(entry).partition("=")
            if sep and (key in generated or key in force) and have.get(key):
                rebuilt.append(f"{key}={have[key]}")
                kept.append(key)
            else:
                rebuilt.append(entry)
        svc["environment"] = rebuilt
    return sorted(set(kept))


def regenerated_elsewhere(doc, installed, generated, kept):
    """Generated secrets whose running value sits under a DIFFERENT service.

    When upstream renames a service (db -> database), the value cannot be
    matched and a fresh password is generated — against the old data volume.
    It does show up in the diff, but among the many lines a rename produces it
    is exactly the one nobody reads. So it gets said out loud.
    """
    existing = installed_env(installed)
    in_new = {}
    for service, svc in (doc.get("services") or {}).items():
        for entry in (svc or {}).get("environment") or []:
            key, sep, _ = str(entry).partition("=")
            if sep:
                in_new.setdefault(key, service)
    notes = []
    for key in sorted(set(generated) - set(kept)):
        for service, values in existing.items():
            if values.get(key) and in_new.get(key) and in_new[key] != service:
                notes.append(
                    f"{key} runs in service '{service}' and the new definition puts it in "
                    f"'{in_new[key]}' — it will be REGENERATED. If '{service}' was renamed "
                    f"upstream, carry the value over by hand (--var {key}=…), otherwise the "
                    f"new value meets old data.")
                break
    return notes


def carry_over_values(doc, variable_names):
    """Installed values for the variables the source asks for: {VAR: value}."""
    known = {}
    for values in installed_env(doc).values():
        for key, value in values.items():
            if key in variable_names and value:
                known.setdefault(key, value)
    return known


def apply_update(host, name, text, user=None, password=None, token=None):
    """PUT the new compose. HTTP 200 here means 'accepted' — nothing more."""
    token = token or login(host, user, password)
    status, raw = api(host, "PUT", f"{INSTALL_PATH}/{name}", token, text, "application/yaml")
    return status, raw, token


def _image_key(image):
    """Comparable form of an image reference: 'redis' and 'docker.io/library/redis:latest' match."""
    text = str(image or "").strip()
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/", "index.docker.io/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if "@" not in text:
        head, _, tail = text.rpartition("/")
        if ":" not in tail:
            text += ":latest"
    return text


def running_containers(host, name, user=None, password=None, token=None):
    """{service: {image, state, status, exit_code, health, id}} of a running app.

    This is the only witness for what an app REALLY runs. The stored compose is
    not: see wait_for_update.
    """
    token = token or login(host, user, password)
    status, raw = api(host, "GET", f"{INSTALL_PATH}/{name}/containers", token)
    if status != 200:
        raise ConvertError(
            f"Container state of '{name}' not readable (HTTP {status}): {raw[:200]}")
    try:
        containers = (json.loads(raw).get("data") or {}).get("containers") or {}
    except json.JSONDecodeError as e:
        raise ConvertError(f"Unexpected container answer for '{name}': {raw[:200]}") from e
    out = {}
    for service, c in containers.items():
        c = c or {}
        out[service] = {"image": c.get("Image") or "", "state": c.get("State") or "",
                        "status": c.get("Status") or "", "exit_code": c.get("ExitCode"),
                        "health": c.get("Health") or "", "id": c.get("ID") or ""}
    return out, token


def running_mismatch(desired, containers):
    """Which services do NOT run what the new compose says. [] means: they all do."""
    problems = []
    wanted = set((desired.get("services") or {}).keys())
    for service in sorted(set(containers) - wanted):
        # A service the new definition no longer has, whose container is still
        # up. Whether ZimaOS removes orphans has not been measured, so this is
        # reported rather than assumed away — a leftover container still holds
        # its ports and its data.
        problems.append(f"{service}: still running ({containers[service].get('status') or '?'}) "
                        f"although the new definition no longer has that service")
    for service, svc in (desired.get("services") or {}).items():
        want = _image_key((svc or {}).get("image"))
        actual = containers.get(service)
        if actual is None:
            problems.append(f"{service}: no container at all")
            continue
        if _image_key(actual["image"]) != want:
            problems.append(f"{service}: runs {actual['image'] or '(none)'}, should run {want}")
        elif actual["state"] != "running":
            problems.append(f"{service}: state '{actual['state'] or '?'}' ({actual['status']})"
                            + (f", exit code {actual['exit_code']}" if actual.get("exit_code") else ""))
    return problems


def wait_for_update(host, name, desired, user=None, password=None, token=None,
                    timeout=180, interval=3, on_progress=None):
    """Wait until the app REALLY runs the new definition.

    Four things were measured on the live system on 2026-07-21, and each of them
    breaks a cheaper signal:

      - HTTP 200 on the PUT means "accepted". A change ZimaOS cannot carry out
        gets exactly the same answer.
      - The app status stays 'running' during a failed update — it never turns
        into an error, because the OLD container keeps running.
      - The stored compose shows the new definition even when it was never
        carried out. In one run it was rolled back after ~7s, in the next it was
        still there after 21s — there is no waiting time that makes this signal
        trustworthy.
      - The app grid is no independent witness: its `image` field flips together
        with the stored compose.

    What does hold: GET .../containers reports the image the container actually
    runs. So the signal is "stored compose matches AND every service runs the
    image it should, in state running".

    Returns a dict with applied / remaining / running_problems / status / waited.
    """
    token = token or login(host, user, password)
    waited, remaining, problems, status = 0, [], [], None
    while True:
        current, status, token = installed_compose(host, name, token=token)
        remaining = compose_diff(current, desired)
        try:
            containers, token = running_containers(host, name, token=token)
            problems = running_mismatch(desired, containers)
        except ConvertError as e:
            problems = [str(e)]
        if on_progress:
            on_progress(waited, status, len(remaining), problems)
        if not remaining and not problems:
            return {"applied": True, "remaining": [], "running_problems": [],
                    "status": status, "waited": waited}
        if waited >= timeout:
            return {"applied": False, "remaining": remaining, "running_problems": problems,
                    "status": status, "waited": waited}
        time.sleep(interval)
        waited += interval


# --- Post-install follow-up -------------------------------------------------
#
# The install API answers "accepted", not "done". Everything that decides
# whether the app is actually usable happens after that, and every one of these
# steps has been seen to fail silently on the live system:
#   - the app never appears in the grid (missing image — uninstall deletes the
#     images of an app, so a locally built tag is gone after a recreate)
#   - the container runs, but the port is unreachable from the LAN (host firewall)
#   - HTTP answers, but with the wrong payload (that is what expectations are for)

def app_state(host, name, user=None, password=None, token=None):
    """(status, port, token) of an app from the grid, or (None, None, token)."""
    token = token or login(host, user, password)
    status, raw = api(host, "GET", "/v2/app_management/web/appgrid", token)
    if status != 200:
        raise ConvertError(f"App grid on {host} returned HTTP {status}: {raw[:200]}")
    for app in json.loads(raw).get("data") or []:
        if (app or {}).get("name") == name:
            return (app.get("status"), str(app.get("port") or "").strip(), token)
    return (None, None, token)


def wait_for_app(host, name, user, password, timeout=300, interval=3, on_progress=None):
    """Poll the grid until the app reports 'running'.

    Returns (ok, status, port, waited_seconds). Does not raise on a timeout —
    the caller wants to report the state, not blow up.
    """
    token, waited, last = None, 0, object()
    while waited <= timeout:
        status, port, token = app_state(host, name, token=token, user=user, password=password)
        if status != last:
            last = status
            if on_progress:
                on_progress(waited, status or "not in the grid yet")
        if status == "running":
            return True, status, port, waited
        time.sleep(interval)
        waited += interval
    return False, (last if isinstance(last, str) else None), None, waited


def http_probe(url, timeout=10):
    """Is something answering there? Any HTTP status counts as reachable.

    A 401 or 302 means the app is up and doing its job — only 'no answer at all'
    is the interesting failure, because that is what a closed firewall port and
    a dead container look like from outside.
    """
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "zimapp"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"{e.reason}"
    except OSError as e:
        return False, f"{e}"


def zfw_active(host, ssh_user):
    """Is the ZFW host firewall running? (True/False/None = cannot tell)

    Deliberately via systemctl and not via a file test: /DATA/zfw is root-only,
    so as a normal user a file check reports 'no ZFW' even when it is active
    (ZIMAOS-KNOWLEDGE.md §13.2.2). None means "no idea" — and that is reported
    as such instead of being turned into a guess.
    """
    if not ssh_user:
        return None
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{ssh_user}@{host}",
         "systemctl is-active zfw-ui.service"],
        capture_output=True, text=True,
    ) if _have_ssh() else None
    if proc is None or proc.returncode not in (0, 3):     # 3 = inactive, still a valid answer
        return None
    return proc.stdout.strip() == "active"


def _have_ssh():
    try:
        subprocess.run(["ssh", "-V"], capture_output=True)
        return True
    except (FileNotFoundError, OSError):
        return False


def missing_images(host, ssh_user, compose_text):
    """Which images referenced by the compose are absent on the host?

    The number one reason an app never shows up: ZimaOS DELETES an app's images
    on uninstall (verified 2026-07-19). For a locally built tag with no registry
    behind it, the next install then has nothing to start from — and the grid
    stays empty without an error anywhere.
    """
    if not ssh_user or not _have_ssh():
        return None
    try:
        doc = yaml.safe_load(compose_text) or {}
    except yaml.YAMLError:
        return None
    wanted = {(svc or {}).get("image") for svc in (doc.get("services") or {}).values()}
    wanted = {i for i in wanted if i}
    if not wanted:
        return None
    try:
        out = ssh_docker(host, ssh_user, ["images", "--format", "{{.Repository}}:{{.Tag}}"])
    except ConvertError:
        return None
    present = set(out.split())
    return sorted(i for i in wanted if i not in present and f"{i}:latest" not in present)


def post_install_check(host, name, user, password, ssh_user=None, compose_text=None,
                       expectations=None, timeout=300, on_progress=None):
    """Everything between "accepted" and "actually usable", as structured steps.

    Each step is {step, ok, detail, hint}. Nothing here raises for a failed
    check — the point is to report the whole picture, including what to do next.
    """
    steps = []

    ok, status, port, waited = wait_for_app(host, name, user, password, timeout=timeout,
                                            on_progress=on_progress)
    steps.append({
        "step": "app in the grid",
        "ok": ok,
        "detail": f"status '{status}' after {waited}s" if status else f"not in the grid after {waited}s",
        "hint": "",
    })
    if not ok:
        gone = missing_images(host, ssh_user, compose_text) if compose_text else None
        if gone:
            steps[-1]["hint"] = (
                f"these images are missing on the host: {', '.join(gone)} — ZimaOS deletes an "
                f"app's images on uninstall, so a locally built tag has to be rebuilt before "
                f"installing again."
            )
        elif gone == []:
            steps[-1]["hint"] = "all images are present — check the app management log on the host."
        else:
            steps[-1]["hint"] = "no SSH available for a deeper diagnosis; check the host directly."
        return steps

    url = f"http://{host}:{port}"
    reachable, detail = http_probe(url)
    step = {"step": f"reachable at {url}", "ok": reachable, "detail": detail, "hint": ""}
    if not reachable:
        active = zfw_active(host, ssh_user)
        if active is True:
            step["hint"] = (f"the ZFW host firewall is active — open port {port} and then run "
                            f"'zfw apply' AND 'zfw commit' (§13.2.2); without apply the rule does nothing.")
        elif active is False:
            step["hint"] = ("ZFW is not active on this host, so the firewall is not the cause — "
                            "check the container itself (docker logs).")
        else:
            step["hint"] = (f"could not determine whether a firewall is in the way (no SSH). If ZFW "
                            f"runs on this host, port {port} needs to be opened + applied + committed (§13.2.2).")
    steps.append(step)

    if expectations:
        results = run_expectations(url, expectations)
        passed = sum(1 for r in results if r["ok"])
        steps.append({
            "step": f"expectations ({passed}/{len(results)})",
            "ok": passed == len(results),
            "detail": "; ".join(f"{'ok' if r['ok'] else 'FAIL'} {r['url']}: {r['detail']}"
                                for r in results),
            "hint": "" if passed == len(results) else "the app answers, but not with what it should.",
        })
    return steps
