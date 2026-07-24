#!/usr/bin/env python3
"""
zimapp_web — the web UI for zimapp_core.

Start:  python3 zimapp.py serve [--port 8790] [--bind 127.0.0.1]

Binds to 127.0.0.1 on purpose: the interface accepts ZimaOS credentials
and loads arbitrary URLs — neither belongs on the LAN unasked.
Anyone who needs it on the LAN has to set --bind 0.0.0.0 explicitly (and then
open the ZFW port, ZIMAOS-KNOWLEDGE.md §13.2.2).
"""

import json
import os
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

import zimapp_core as core

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
# Main carries update/drift and the framework checks, which the released
# v2.0.0 image does not. Saying "v2.0" here would make a checkout look like
# the published image — the suffix says which side of the tag you are on.
VERSION = "v2.1-dev"
# The request body carries the JSON envelope around an opened file, and escaping
# newlines/quotes inflates it well beyond the file size. With both limits at 2 MB a
# 1.9 MB file was accepted by the browser ("the limit is 2 MB") and then rejected
# with 413 by the server — two messages contradicting each other, and no way to
# convert that file at all (review 2026-07-21). The server ceiling is now well
# above the file ceiling the UI advertises (MAX_OPEN_FILE in app.js).
MAX_BODY = 8 * 1024 * 1024

CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml"}


def defaults():
    return {
        "host": os.environ.get("ZIMA_HOST", ""),
        "ssh_user": os.environ.get("ZIMA_SSH_USER", ""),
        "user": os.environ.get("ZIMA_USER", ""),
        "version": VERSION,
        "blueprints": core.list_blueprints(),
    }


def _used_ports(options):
    """Fetch the taken host ports from the target — only when explicitly requested.

    Two sources, because neither alone is enough: the app grid API needs
    credentials but no SSH — so it works from inside a container, where there
    is no key — while docker-over-SSH also sees foreign containers but needs
    that key. If a source fails, it is a real error and is passed upwards
    instead of silently becoming an empty set: otherwise zimapp silently
    proposes a port that is already taken.

    Returns (ports, notes); the notes say which source actually answered.
    """
    if not options.get("check_ports"):
        return set(), []
    host = options.get("host")
    if not host:
        raise core.ConvertError("Port check requested, but no host given.")
    return core.collect_used_ports(
        host,
        ssh_user=options.get("ssh_user") or None,
        user=options.get("user") or os.environ.get("ZIMA_USER"),
        password=options.get("password") or os.environ.get("ZIMA_PASS"),
    )


# --- API handlers -----------------------------------------------------------

# What a blueprint may contribute to the form's metadata. `index`, `image`, `port`
# and `author` are in here because save_blueprint writes them: a field that is
# stored but never read back makes a saved Dockerfile recipe unusable, and says
# nothing about it (review 2026-07-21).
BLUEPRINT_META_KEYS = ("name", "title", "category", "tagline", "description", "author",
                       "icon", "memory", "cpus", "main", "index", "image", "port")


def _one_source_only(payload):
    """Refuse a request that carries more than one source.

    The UI is supposed to keep file, URL and blueprint mutually exclusive, and it
    said so — but a blueprint selection writes the URL field programmatically,
    which fires no `input` event, so the clearing never ran (review 2026-07-21).
    The result was silent: the opened file supplied the YAML while the URL decided
    where `env_file` was fetched from, mixing a foreign project's passwords into
    the generated compose. A client-side rule that is only enforced client-side is
    not a rule, so the server refuses the ambiguity instead of picking a winner.
    """
    if not payload.get("text"):
        return
    other = []
    if (payload.get("url") or "").strip():
        other.append("a URL")
    if payload.get("blueprint"):
        other.append("a blueprint")
    if other:
        raise core.ConvertError(
            "The request carries an opened file and " + " and ".join(other) + " at "
            "once. They contradict each other: the file supplies the YAML while the "
            "URL decides where an env_file is read from, so a foreign project's "
            "values would end up in this compose. Pick one source. Nothing was converted."
        )


def api_analyze(payload):
    _one_source_only(payload)
    # A file opened in the browser sends its content along: the path the user sees
    # exists on their machine, not in this container, so there is nothing to fetch.
    if payload.get("text"):
        text = payload["text"]
        if not text.strip():
            raise core.ConvertError("The opened file is empty.")
        effective = payload.get("filename") or "(opened file)"
    else:
        text, effective = core.fetch_source(payload["url"])
    kind = core.detect_kind(text)

    # Read along the .env next to the source: otherwise the UI claims "mandatory"
    # for variables that already come from that file when generating.
    env_values, env_source = ({}, None)
    names = core.env_file_names(text) if kind == "compose" else []
    if names or "${" in text:
        env_values, env_source = core.fetch_env_files(effective, names)

    variables = core.find_variables(text)
    for var in variables:
        if var["name"] in env_values:
            # The env file wins when generating — build_from_source merges it over
            # the inline ${VAR:-default}, which is what compose itself does with a
            # .env. So it has to win here too: showing the inline default while the
            # generated image says something else is a display that lies.
            # (immich: the form said 'release', the image tag was ':v3'.)
            if var["default"] is not None and var["default"] != env_values[var["name"]]:
                var["inline_default"] = var["default"]
            var["default"] = env_values[var["name"]]
            var["from_env_file"] = env_source

    result = {
        "kind": kind,
        "source": effective,
        "variables": variables,
        "env_source": env_source,
        "warnings": [],
        "services": [],
        "dockerfile": None,
        "suggested_main": None,
    }

    if kind == "compose":
        resolved, _ = core.resolve_variables(text, {}, autofill_secrets=False)
        try:
            doc = core.yaml.safe_load(resolved)
        except core.yaml.YAMLError as e:
            raise core.ConvertError(f"The compose YAML is not parsable: {e}")
        if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
            raise core.ConvertError("No 'services:' block found.")
        services = doc["services"]
        result["services"] = core.analyze(doc)
        first = next(iter(services))
        app_id = core.slugify(core.app_name_from_image((services[first] or {}).get("image"), first))
        try:
            result["suggested_main"] = core.pick_main_service(services, app_id)
        except core.ConvertError as e:
            result["warnings"].append(str(e))
            result["suggested_main"] = first
        main_image = (services[result["suggested_main"]] or {}).get("image")
        stem = core.app_name_from_image(main_image, result["suggested_main"])
        result["suggested_name"] = core.slugify(stem)
        result["suggested_title"] = stem.replace("-", " ").replace("_", " ").title()
        if env_source and env_values and names:
            result["warnings"].append(
                f"env_file found and read along: {env_source} — {len(env_values)} values "
                f"land directly in 'environment', because ZimaOS does not create a .env."
            )
        elif env_source and env_values:
            # No service declares env_file, so convert() writes none of these into
            # 'environment' — it only merges them where a ${VAR} is substituted.
            # Claiming otherwise made people believe settings had been applied.
            result["warnings"].append(
                f"{env_source} was read next to the source, but no service declares "
                f"'env_file' — so these {len(env_values)} values do NOT go into "
                f"'environment'. They only fill in the ${{VARIABLES}} below; anything "
                f"the app expects as an environment variable has to be set by hand."
            )
        elif env_source:
            result["warnings"].append(
                f"{env_source} found, but it contains no values that are set (only comments). "
                f"Whatever the app needs has to be set by hand as a variable below."
            )
        elif names and payload.get("text"):
            # "could not be found next to it" would suggest we looked. With an
            # opened file there is no directory to look in.
            result["warnings"].append(
                f"The file refers to an env_file ({', '.join(names)}), and an opened file "
                f"has no directory to look next to — nothing was searched. ZimaOS creates "
                f"no .env either, so those values have to be set by hand below."
            )
        elif names:
            result["warnings"].append(
                "The source refers to an env_file that could not be found next to it — "
                "ZimaOS does not create a .env, the values have to be set by hand."
            )
        for row in result["services"]:
            if row["build"]:
                result["warnings"].append(
                    f"Service '{row['name']}' has 'build:' — ZimaOS does not build images (§4.4.1); "
                    f"only the referenced 'image:' counts."
                )
    else:
        df = core.parse_dockerfile(text)
        result["dockerfile"] = df
        result["suggested_name"] = "app"
        result["suggested_title"] = "App"
        result["suggested_main"] = "app"
        result["services"] = [{"name": "app", "image": None, "build": True,
                               "ports": [str(p) for p in df["ports"]],
                               "volumes": len(df["volumes"]), "role": "app", "depends_on": []}]
        result["warnings"].append(
            "Dockerfile source: ZimaOS only installs prebuilt images — please state below the "
            "image name under which the built image is reachable."
        )

    # Propose an icon that actually exists instead of prefilling the form with a
    # placeholder: a placeholder is reachable, passes the Rule 8 check and then
    # sits in the grid as a foreign brand without anyone noticing.
    result["suggested_icon"], tried = core.suggest_icon(result["suggested_name"])
    if not result["suggested_icon"]:
        result["warnings"].append(
            f"No icon found for '{result['suggested_name']}' — tried: "
            f"{', '.join(url for url, _ in tried)}. The field stays empty on purpose; "
            f"please set one by hand, otherwise the tile stays blank (Rule 8)."
        )
    return result


def api_generate(payload):
    _one_source_only(payload)
    options = payload.get("options") or {}
    meta = dict(payload.get("meta") or {})
    variables = dict(payload.get("variables") or {})
    url = (payload.get("url") or "").strip() or None

    blueprint = None
    if payload.get("blueprint"):
        blueprint = core.load_blueprint(payload["blueprint"])
        blueprint_url = core.blueprint_source_url(blueprint)[0]
        if url and url != blueprint_url:
            # The UI echoes the blueprint's source into the URL field, so these
            # normally agree. If they do not, the request says two different things
            # and guessing which one is meant is how the wrong compose gets built.
            raise core.ConvertError(
                f"Blueprint '{blueprint['name']}' points at {blueprint_url}, but the "
                f"request also carries {url}. Clear one of the two — nothing was converted."
            )
        url = blueprint_url
        variables = dict(blueprint.get("vars") or {}, **variables)
        # Only fill what the form left empty — what the user typed always wins.
        for key in BLUEPRINT_META_KEYS:
            if blueprint.get(key) and not meta.get(key):
                meta[key] = blueprint[key]

    used_ports, port_notes = _used_ports(options)
    yaml_text, info = core.build_from_source(
        url, meta, variables,
        {"autofill_secrets": options.get("autofill_secrets", True),
         "used_ports": used_ports,
         "check_icon": options.get("check_icon", True),
         "source_text": payload.get("text"),
         "source_name": payload.get("filename")},
    )

    if blueprint and blueprint.get("env"):
        # Only now are the host port and app id fixed — values such as
        # PAPERLESS_URL need exactly those.
        doc = core.yaml.safe_load(yaml_text)
        info["generated"].update(core.apply_blueprint_env(
            doc, blueprint, options.get("host"), info["web_port"], info["app_id"]))
        yaml_text = core.dump(doc)
        info["problems"], extra = core.validate(yaml_text)
        info["warnings"] += extra

    return {"yaml": yaml_text, "main": info["main"], "web_port": info["web_port"],
            "app_id": info["app_id"], "generated": info["generated"],
            "problems": info["problems"],
            "blueprint": blueprint["name"] if blueprint else None,
            # The port notes belong in the warnings: which source answered
            # decides how much the check is actually worth.
            "warnings": info["warnings"] + port_notes}


def api_blueprint_save(payload):
    """Save the current form as a blueprint (delta + source), not as a compose copy."""
    meta = dict(payload.get("meta") or {})
    data = dict(meta)
    data["source"] = payload.get("url") or ""
    data["vars"] = payload.get("variables") or {}
    path, warnings = core.save_blueprint(data, overwrite=bool(payload.get("overwrite")))
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return {"path": path, "yaml": text, "warnings": warnings,
            "blueprints": core.list_blueprints()}


def api_verify(payload):
    """Run a blueprint's expectations against a running installation."""
    blueprint = core.load_blueprint(payload.get("name") or "")
    expectations = blueprint.get("expect") or []
    if not expectations:
        raise core.ConvertError(
            f"Blueprint '{blueprint['name']}' has no expect block — there is nothing to verify.")

    base, app_status = payload.get("url"), None
    if not base:
        host, user, password = _credentials(payload)
        base, app_status = core.app_base_url(host, blueprint["name"], user, password)

    results = core.run_expectations(base, expectations)
    return {
        "base": base,
        "app_status": app_status,
        "results": [{"url": r["url"], "ok": r["ok"], "detail": r["detail"]} for r in results],
        "passed": sum(1 for r in results if r["ok"]),
        "total": len(results),
    }


def api_validate(payload):
    text = payload.get("yaml") or ""
    problems, warnings = core.validate(text)
    main = port_map = None
    try:
        doc = core.yaml.safe_load(text)
        casaos = (doc or {}).get("x-casaos") or {}
        main, port_map = casaos.get("main"), casaos.get("port_map")
    except core.yaml.YAMLError:
        pass
    return {"problems": problems, "warnings": warnings, "main": main, "port_map": port_map}


def _credentials(payload):
    host = payload.get("host") or os.environ.get("ZIMA_HOST")
    user = payload.get("user") or os.environ.get("ZIMA_USER")
    password = payload.get("password") or os.environ.get("ZIMA_PASS")
    if not host:
        raise core.ConvertError("No host given.")
    if not user or not password:
        raise core.ConvertError("Credentials are missing — give a user and a password (or set ZIMA_USER/ZIMA_PASS).")
    return host, user, password


def api_install(payload):
    text = payload.get("yaml") or ""
    problems, _ = core.validate(text)
    if problems:
        raise core.ConvertError("Installation aborted, the compose file is not compliant:\n- " + "\n- ".join(problems))
    host, user, password = _credentials(payload)
    status, body = core.install(host, user, password, text)
    return {"status": status, "body": body}


def api_postcheck(payload):
    """The follow-up after an install — same checks as the CLI runs."""
    host, user, password = _credentials(payload)
    name = payload.get("name")
    if not name:
        raise core.ConvertError("No app ID given.")
    expectations = None
    if os.path.isfile(core.blueprint_path(name)):
        expectations = core.load_blueprint(name).get("expect")
    steps = core.post_install_check(
        host, name, user, password,
        ssh_user=payload.get("ssh_user") or None,
        compose_text=payload.get("yaml"),
        expectations=expectations,
        timeout=int(payload.get("timeout") or 180),
        # Shorter than the CLI default on purpose: this one blocks a browser
        # request. A 5xx that is still there after a minute is reported as a
        # 5xx instead of holding the request open until the app finally starts.
        serve_timeout=int(payload.get("serve_timeout") or 60),
    )
    return {"steps": steps, "ok": all(s["ok"] for s in steps)}


def _update_target(payload):
    """(host, user, password, name, text, desired) for both update endpoints.

    The app name comes out of the compose itself. Taking it from a separate
    field would allow sending app A's definition to app B — and an update never
    renames, it would just overwrite the wrong app.
    """
    host, user, password = _credentials(payload)
    text = payload.get("yaml") or ""
    problems, _ = core.validate(text)
    if problems:
        raise core.ConvertError(
            "Nothing was sent, the compose is not compliant:\n- " + "\n- ".join(problems))
    desired = core.yaml.safe_load(text)
    name = str((desired or {}).get("name") or "").strip()
    if not name:
        # A second lock. validate() above already refuses a compose without a
        # top-level name (Rule 4), so this normally never fires — but a PUT to
        # an empty app name is not something to leave to another function's
        # promise.
        raise core.ConvertError("The compose has no top-level 'name:' — no app to update.")
    return host, user, password, name, text, desired


def api_update_diff(payload):
    """What would change on the installation. Reads, sends nothing."""
    host, user, password, name, _, desired = _update_target(payload)
    installed, status, _ = core.installed_compose(host, name, user, password)
    return {"name": name, "app_status": status,
            "changes": core.compose_diff(installed, desired),
            "not_compared": core.SERVICE_X_CASAOS_NOTE}


def api_update_apply(payload):
    """Send it, then wait until the app REALLY runs it.

    Blocking on purpose, like /api/postcheck: the answer has to be the outcome,
    not a receipt. HTTP 200 from ZimaOS means 'accepted', and a change it cannot
    carry out looks exactly the same from there.
    """
    host, user, password, name, text, desired = _update_target(payload)
    installed, _, token = core.installed_compose(host, name, user, password)
    changes = core.compose_diff(installed, desired)
    if not changes:
        return {"name": name, "sent": False, "applied": True, "changes": [],
                "message": f"'{name}' already matches — nothing was sent."}
    status, body, token = core.apply_update(host, name, text, token=token)
    if status != 200:
        raise core.ConvertError(f"ZimaOS refused the change (HTTP {status}): {body[:300]}")
    result = core.wait_for_update(host, name, desired, token=token,
                                  timeout=int(payload.get("timeout") or 180))
    return {"name": name, "sent": True, "changes": changes, "http": status,
            "applied": result["applied"], "waited": result["waited"],
            "remaining": result["remaining"],
            "running_problems": result["running_problems"],
            "not_compared": core.SERVICE_X_CASAOS_NOTE}


def api_uninstall(payload):
    host, user, password = _credentials(payload)
    name = payload.get("name")
    if not name:
        raise core.ConvertError("No app ID given.")
    status, body = core.uninstall(host, user, password, name)
    return {"status": status, "body": body}


ROUTES = {
    "/api/analyze": api_analyze,
    "/api/verify": api_verify,
    "/api/postcheck": api_postcheck,
    "/api/generate": api_generate,
    "/api/blueprint/save": api_blueprint_save,
    "/api/validate": api_validate,
    "/api/install": api_install,
    "/api/update/diff": api_update_diff,
    "/api/update/apply": api_update_apply,
    "/api/uninstall": api_uninstall,
}


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "zimapp/" + VERSION

    def log_message(self, fmt, *args):  # one line per request, without noise
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    _head_only = False

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not self._head_only:      # HEAD: headers yes, body no
            self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False))

    def do_HEAD(self):
        """Answer HEAD like GET, only without a body.

        Without this, BaseHTTPRequestHandler answers 501 — and that is exactly
        what every reachability check of our own icon fails on (Rule 8),
        even though the file is served flawlessly.
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/defaults":
            # Same guard as on POST: without it an exception in here closes the
            # connection with no body at all, and the UI just sees "no JSON"
            # with nothing to go on.
            try:
                return self._json(200, defaults())
            except Exception as e:  # noqa: BLE001 — never swallow, always report
                import traceback
                traceback.print_exc()
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})

        name = "index.html" if path == "/" else os.path.basename(path)
        full = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(full):
            return self._json(404, {"error": f"Not found: {path}"})
        ext = os.path.splitext(name)[1]
        with open(full, "rb") as fh:
            self._send(200, fh.read(), CONTENT_TYPES.get(ext, "application/octet-stream"))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if not handler:
            return self._json(404, {"error": f"Unknown endpoint {path}"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "Request body too large."})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"Invalid JSON: {e}"})

        try:
            return self._json(200, handler(payload))
        except core.ConvertError as e:
            # Domain error: 400 + plain text, so that the UI can display it.
            return self._json(400, {"error": str(e)})
        except KeyError as e:
            return self._json(400, {"error": f"Field missing in the request: {e}"})
        except Exception as e:  # noqa: BLE001 — do not swallow anything
            import traceback
            traceback.print_exc()
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def serve(bind="127.0.0.1", port=8790):
    httpd = ThreadingHTTPServer((bind, port), Handler)
    print(f"zimapp {VERSION} — http://{bind}:{port}", file=sys.stderr)
    if bind not in ("127.0.0.1", "localhost"):
        print("WARNING: bound to %s — the UI accepts ZimaOS credentials." % bind,
              file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        httpd.server_close()
