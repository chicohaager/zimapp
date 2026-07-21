#!/usr/bin/env python3
"""
zimapp — turns a compose URL (or a Docker image) into a
ZimaOS-compliant app and installs it via the official API.

Why it exists: ZimaOS expects a compose file with an x-casaos block whose
quirks are documented nowhere and which you otherwise have to piece together
by trial and error (port_map MUST be a string, decimal numbers MUST have a
period, data belongs under /DATA/AppData/<app>/ …).
Every rule here is verified on the live system on ZimaOS v1.7.0-beta1 —
see RULES below and ZIMAOS-KNOWLEDGE.md §3.4/§4.4.1/§5.

Usage:
    zimapp.py serve                                    # web UI on 127.0.0.1:8790
    zimapp.py convert https://…/docker-compose.yml --title "Nextcloud" > app.yml
    zimapp.py inspect  nginx:alpine --host 192.168.1.100
    zimapp.py generate nginx:alpine --host 192.168.1.100 --title "Webserver" > app.yml
    zimapp.py validate app.yml
    zimapp.py install  app.yml --host 192.168.1.100
    zimapp.py update   my-webserver --blueprint my-webserver     # diff; --apply to apply
    zimapp.py drift                                              # has upstream moved?
    zimapp.py uninstall my-webserver --host 192.168.1.100

Credentials come from the environment (ZIMA_USER / ZIMA_PASS) or --user/--pass.
"""

import argparse
import json
import os
import re
import sys

import zimapp_core as core
from zimapp_core import ConvertError

# --- RULES, all verified live on v1.7.0-beta1 -------------------------------
#
# 1) Auth: the JWT belongs RAW in the Authorization header. With a "Bearer "
#    prefix ZimaOS answers "invalid or expired jwt" — which looks like an
#    expired token, but is a header format error.
# 2) Installation: POST /v2/app_management/compose, Content-Type application/yaml,
#    body = raw compose YAML. Answer 200 = "the app is being installed asynchronously".
# 3) Uninstallation: DELETE /v2/app_management/compose/{name}?delete_config_folder=true
# 4) The compose file needs a top-level "name:" — that is the app ID.
# 5) port_map MUST be a string ("8080"). An int breaks the parser, the app is
#    afterwards missing from the grid without comment.
# 6) Decimal numbers (deploy.resources.*.memory/cpus) MUST have a period as the
#    separator. The ZimaOS-own UI generates "14,92GB" in a German locale
#    and fails on that itself (strconv.ParseFloat, HTTP 400).
# 7) Persistent data belongs under /DATA/AppData/<app>/ — that is the only
#    anchor that the ZimaOS redirections act on.
# 8) icon has to be a reachable URL, otherwise the tile stays empty.
#
# Multi-service stacks (app + database) are ONE app: x-casaos.main points at
# the WebUI service, port_map at its host port, all services hang off
# a shared bridge network (§3.4/§4.4.1).


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def credentials(args):
    user = args.user or os.environ.get("ZIMA_USER")
    password = getattr(args, "password", None) or os.environ.get("ZIMA_PASS")
    if not user or not password:
        die("Credentials are missing — set ZIMA_USER and ZIMA_PASS or use --user/--pass.")
    return user, password


def meta_from_args(args, fallback_title=None):
    return {
        "name": args.name,
        "title": args.title or fallback_title,
        "main": getattr(args, "main", None),
        "author": args.author,
        "category": args.category,
        "tagline": args.tagline,
        "description": args.description,
        "icon": args.icon,
        "memory": args.memory,
        "cpus": args.cpus,
        "image": getattr(args, "image_ref", None),
        "port": getattr(args, "port", None),
    }


def report(problems, warnings, stream=sys.stderr):
    for w in warnings:
        print(f"  Note: {w}", file=stream)
    for p in problems:
        print(f"  ERROR:  {p}", file=stream)


# --- Commands ---------------------------------------------------------------

def cmd_convert(args):
    """Compose/Dockerfile URL → ZimaOS compose (multi-service capable)."""
    blueprint = None
    if args.blueprint:
        blueprint = core.load_blueprint(args.blueprint, allow_paths=True)
        url, pinned = core.blueprint_source_url(blueprint)
        args.url = url
        # Blueprint values only fill gaps — an explicit flag always wins, so a
        # blueprint never silently overrides what the user typed.
        for flag, key in (("name", "name"), ("title", "title"), ("category", "category"),
                          ("tagline", "tagline"), ("description", "description"),
                          ("icon", "icon"), ("memory", "memory"), ("cpus", "cpus"),
                          ("main", "main")):
            if blueprint.get(key) and getattr(args, flag, None) in (None, PARSER_DEFAULTS.get(flag)):
                setattr(args, flag, blueprint[key])
        print(f"Blueprint: {blueprint['_path']}"
              + (" (pinned)" if pinned else " (source: moving branch, not pinned)"), file=sys.stderr)
        if blueprint.get("verified"):
            v = blueprint["verified"]
            print(f"  last verified {v.get('date')} on {v.get('host')}", file=sys.stderr)

    used = set()
    if args.check_ports:
        # Credentials are optional here: without them the app grid API is
        # skipped and only the SSH view remains (and vice versa). Which
        # source actually answered is printed, so nobody mistakes a partial
        # check for a complete one.
        user = args.user or os.environ.get("ZIMA_USER")
        password = getattr(args, "password", None) or os.environ.get("ZIMA_PASS")
        used, notes = core.collect_used_ports(
            args.host, ssh_user=args.ssh_user, user=user, password=password,
            sources=args.port_source_list,
        )
        for note in notes:
            print(f"  Port check: {note}", file=sys.stderr)

    variables = {}
    for item in args.var or []:
        if "=" not in item:
            die(f"--var expects NAME=VALUE, got: {item}")
        key, _, value = item.partition("=")
        variables[key] = value

    if blueprint:
        variables = dict(blueprint.get("vars") or {}, **variables)

    text, info = core.build_from_source(
        args.url, meta_from_args(args), variables,
        {"autofill_secrets": not args.no_secrets, "used_ports": used,
         "check_icon": not args.no_icon_check},
    )

    if blueprint and blueprint.get("env"):
        # Only now are the host port and the app id fixed — values like
        # PAPERLESS_URL need exactly those, so this runs after the conversion.
        doc = core.yaml.safe_load(text)
        info["generated"].update(core.apply_blueprint_env(
            doc, blueprint, args.host, info["web_port"], info["app_id"]))
        text = core.dump(doc)
        info["problems"], extra = core.validate(text)
        info["warnings"] += extra

    print(f"Source: {info['source']} ({info['kind']})", file=sys.stderr)
    print(f"WebUI service: {info['main']} → host port {info['web_port']}", file=sys.stderr)
    if info["generated"]:
        print("Generated secrets (only visible here!):", file=sys.stderr)
        for key, value in info["generated"].items():
            print(f"  {key}={value}", file=sys.stderr)
    report(info["problems"], info["warnings"])
    if info["problems"]:
        die("the generated compose file is not ZimaOS-compliant — see the errors above.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"written: {args.out}", file=sys.stderr)
    else:
        print(text, end="")


def cmd_inspect(args):
    print(json.dumps(core.inspect_image(args.image, args.host, args.ssh_user),
                     indent=2, ensure_ascii=False))


def cmd_generate(args):
    """A single image → ZimaOS compose (the original path)."""
    if args.offline:
        # Without inspection we do not know the EXPOSE ports — then the
        # WebUI port has to be named, otherwise there would be no port_map and the
        # tile would stay dead (Rule 5).
        if not args.port:
            die("--offline without --port: the WebUI container port is unknown. "
                "Either set --port <container-port> or let the inspection run.")
        image_meta = {"ports": [args.port], "volumes": [], "env": []}
    else:
        image_meta = core.inspect_image(args.image, args.host, args.ssh_user)

    doc = {"services": {"app": {
        "image": args.image,
        "ports": [f"{p}:{p}" for p in image_meta["ports"]],
        "volumes": list(image_meta["volumes"]),
        "environment": list(image_meta["env"]),
    }}}

    used = set() if args.offline else core.used_host_ports(args.host, args.ssh_user)
    meta = meta_from_args(args, fallback_title=core.app_name_from_image(args.image))
    meta.setdefault("name", None)
    if not meta["name"] and not meta["title"]:
        meta["name"] = core.slugify(core.app_name_from_image(args.image))

    result, info = core.convert(doc, meta, {"used_ports": used})
    text = core.dump(result)
    problems, warnings = core.validate(text)
    report(problems, info["warnings"] + warnings)
    if problems:
        die("the generated compose file is not ZimaOS-compliant — please report this, it is a bug in zimapp.")
    print(text, end="")


def cmd_validate(args):
    with open(args.file, encoding="utf-8") as fh:
        problems, warnings = core.validate(fh.read())
    for w in warnings:
        print(f"Note: {w}")
    for p in problems:
        print(f"ERROR:  {p}")
    if problems:
        sys.exit(1)
    print("OK — meets all the checked ZimaOS requirements.")


def cmd_install(args):
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    problems, _ = core.validate(text)
    if problems:
        report(problems, [])
        die("Installation aborted — repair the compose file first.")

    user, password = credentials(args)
    status, raw = core.install(args.host, user, password, text)
    print(f"HTTP {status}: {raw[:300]}")
    if status != 200:
        sys.exit(1)
    name = re.search(r"^name:\s*(\S+)", text, re.M).group(1)
    if args.no_wait:
        print(f"The installation runs asynchronously. To check:\n"
              f"  ssh {args.ssh_user}@{args.host} 'DOCKER_CONFIG={core.DOCKER_CONFIG_DIR} docker ps | grep {name}'")
        return

    # HTTP 200 only means "accepted". Everything that decides whether the app is
    # usable happens after this — so wait for it instead of leaving the user to
    # find out later that nothing ever came up.
    print("Waiting for the app (HTTP 200 means accepted, not done)…")

    expectations = None
    try:
        expectations = (core.load_blueprint(name, allow_paths=True).get("expect")
                        if os.path.isfile(core.blueprint_path(name, allow_paths=True)) else None)
    except ConvertError:
        expectations = None

    steps = core.post_install_check(
        args.host, name, user, password, ssh_user=args.ssh_user, compose_text=text,
        expectations=expectations, timeout=args.wait_timeout,
        on_progress=lambda waited, status: print(f"  t+{waited:>4}s  {status}"),
    )
    failed = 0
    for s in steps:
        print(f"  [{'ok  ' if s['ok'] else 'FAIL'}] {s['step']} — {s['detail']}")
        if s["hint"]:
            print(f"         → {s['hint']}")
        failed += 0 if s["ok"] else 1
    if failed:
        die(f"{failed} check(s) failed — the app is installed but not usable as it stands.")
    print("The app is up and reachable." + (" All expectations hold." if expectations else ""))


def cmd_uninstall(args):
    user, password = credentials(args)
    status, raw = core.uninstall(args.host, user, password, args.name)
    print(f"HTTP {status}: {raw[:300]}")
    if status == 200:
        # Verified 2026-07-19: ZimaOS deletes the app's images along with it.
        # For a locally built tag with no registry behind it, the next install
        # has nothing left to start from — and fails without an error anywhere.
        print("Note: ZimaOS also deletes this app's images. If one of them was built "
              "locally (no registry), rebuild it BEFORE installing again.")
    sys.exit(0 if status == 200 else 1)


def _update_source(args, installed):
    """Where the new definition comes from — never guessed.

    Order: an explicit file, an explicit URL, an explicit blueprint, a blueprint
    that carries the app's own name. If none of those exist, the command says so
    instead of inventing a source.
    """
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            return fh.read(), f"file {args.file}", None
    blueprint = None
    if args.blueprint:
        blueprint = core.load_blueprint(args.blueprint, allow_paths=True)
    elif not args.source and os.path.isfile(core.blueprint_path(args.name)):
        blueprint = core.load_blueprint(args.name)
        print(f"Using the blueprint of the same name: {blueprint['_path']}", file=sys.stderr)
    url = args.source
    if blueprint:
        url, pinned = core.blueprint_source_url(blueprint)
        print(f"Blueprint source: {url}" + (" (pinned)" if pinned else " (moving branch)"),
              file=sys.stderr)
    if not url:
        die(f"No source for '{args.name}'. Say where the new definition comes from: "
            f"--source <URL>, --blueprint <name>, or --file <compose.yml>. "
            f"There is no blueprint called '{args.name}' to fall back on.")

    meta = core.meta_from_installed(installed)
    if blueprint:
        for key in ("title", "category", "tagline", "description", "icon",
                    "memory", "cpus", "main"):
            if blueprint.get(key):
                meta[key] = blueprint[key]
    for key in ("title", "author", "category", "tagline", "description", "icon",
                "memory", "cpus", "main"):
        value = getattr(args, key, None)
        if value:
            meta[key] = value
    meta["name"] = args.name          # update never renames — that would be a new app

    # Values that already run must survive. A freshly generated POSTGRES_PASSWORD
    # against an existing database volume means the app never comes up again.
    source_text, _ = core.fetch_source(url)
    names = {v["name"] for v in core.find_variables(source_text)}
    variables = core.carry_over_values(installed, names)
    kept = sorted(variables)
    if blueprint:
        variables.update(blueprint.get("vars") or {})
    for item in args.var or []:
        if "=" not in item:
            die(f"--var expects NAME=VALUE, got: {item}")
        key, _, value = item.partition("=")
        variables[key] = value
    if kept:
        print(f"Kept from the installation: {', '.join(kept)}", file=sys.stderr)

    text, info = core.build_from_source(
        url, meta, variables,
        {"autofill_secrets": True, "check_icon": not args.no_icon_check},
    )
    doc = core.yaml.safe_load(text)
    if blueprint and blueprint.get("env"):
        info["generated"].update(core.apply_blueprint_env(
            doc, blueprint, args.host, info["web_port"], info["app_id"]))

    # A regenerated password is not a new password, it is a broken app. Values
    # that already run win over anything that was generated here.
    restored = core.keep_installed_values(doc, installed, info["generated"],
                                          force=args.keep or ())
    for key in restored:
        info["generated"].pop(key, None)
    text = core.dump(doc)
    info["problems"], extra = core.validate(text)
    info["warnings"] += extra

    if restored:
        print(f"Kept from the installation instead of regenerating: {', '.join(restored)}",
              file=sys.stderr)
    for note in core.regenerated_elsewhere(doc, installed, info["generated"], restored):
        info["warnings"].append(note)
    if info["generated"]:
        print("Newly generated secrets (they exist nowhere else — note them down):", file=sys.stderr)
        for key, value in info["generated"].items():
            print(f"  {key}={value}", file=sys.stderr)
    report(info["problems"], info["warnings"])
    if info["problems"]:
        die("the newly generated compose is not ZimaOS-compliant — see the errors above.")
    return text, info["source"], info


def cmd_update(args):
    """Change an installed app in place: read back → compare → apply.

    The starting point is what ZimaOS actually has, not a file lying around
    here: a `uninstall` + `install` round would delete the app's images and its
    data directory, and there is no error anywhere when that goes wrong.
    """
    # Argument combinations first, before anything touches the network: a
    # finished file IS the definition, so flags that could only act on a
    # generated one would be a silent no-op — the user would believe a value was
    # kept that nothing ever looked at.
    if args.file:
        ignored = [flag for flag, value in (("--source", args.source), ("--blueprint", args.blueprint),
                                            ("--var", args.var), ("--keep", args.keep)) if value]
        if ignored:
            die(f"--file is the finished definition; {', '.join(ignored)} would do nothing on "
                f"that path. Either edit the file, or use --source/--blueprint so the compose "
                f"is generated and those values have somewhere to apply.")

    user, password = credentials(args)
    installed, status, token = core.installed_compose(args.host, args.name, user, password)
    print(f"Installed: {args.name} — status {status}", file=sys.stderr)

    text, origin, _ = _update_source(args, installed)
    problems, warnings = core.validate(text)
    report(problems, warnings)
    if problems:
        die("the new compose is not ZimaOS-compliant — nothing was sent.")

    desired = core.yaml.safe_load(text)
    if desired.get("name") != args.name:
        die(f"The new compose calls itself '{desired.get('name')}', the app is '{args.name}'. "
            f"An update never renames — that would install a second app.")

    changes = core.compose_diff(installed, desired)
    print(f"\nSource: {origin}")
    if not changes:
        print(f"No differences — '{args.name}' already matches its source.")
        print(f"({core.SERVICE_X_CASAOS_NOTE}.)")
        return
    print(f"{len(changes)} difference(s) between the installation and the new definition:")
    for c in changes:
        mark = {"add": "+", "remove": "-", "change": "~"}[c["kind"]]
        old = "(not set)" if c["installed"] is None else repr(c["installed"])
        new = "(removed)" if c["desired"] is None else repr(c["desired"])
        print(f"  {mark} {c['path']}\n      {old}  ->  {new}")
    print(f"\nNot compared: {core.SERVICE_X_CASAOS_NOTE}.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"written: {args.out}", file=sys.stderr)
    if not args.apply:
        print("\nNothing was sent (dry run). Add --apply to apply it.")
        sys.exit(2)          # 2 = there are differences, for scripted drift checks

    status_code, raw, token = core.apply_update(args.host, args.name, text, token=token)
    print(f"\nHTTP {status_code}: {raw[:200]}")
    if status_code != 200:
        die("ZimaOS refused the change.")

    # HTTP 200 is 'accepted'. A change ZimaOS cannot carry out gets the same
    # answer and even appears in the stored compose, while the old container
    # keeps running and the status keeps saying 'running' (measured 2026-07-21).
    # So wait for the containers, not for the document.
    print("Waiting until the app really runs it (200 means accepted, not done)…")
    result = core.wait_for_update(
        args.host, args.name, desired, token=token, timeout=args.wait_timeout,
        on_progress=lambda w, s, n, probs: print(
            f"  t+{w:>4}s  status {s}, {n} difference(s) in the stored compose"
            + (f", {len(probs)} service(s) not running it yet" if probs else ", containers match")),
    )
    if not result["applied"]:
        if result["remaining"]:
            print(f"\nStored compose still differs after {result['waited']}s:", file=sys.stderr)
            for c in result["remaining"]:
                print(f"  ~ {c['path']}: {c['installed']!r} != {c['desired']!r}", file=sys.stderr)
        for problem in result["running_problems"]:
            print(f"  RUNNING: {problem}", file=sys.stderr)
        die("The app does not run the new definition. Nothing was destroyed — the old "
            "container is still up, which is exactly why neither the app status nor the "
            "stored compose shows a problem. The usual cause is an image that cannot be "
            "pulled: check the tag, and for a locally built image that it still exists on "
            "the host (uninstall deletes images). Logs: "
            f"GET /v2/app_management/compose/{args.name}/logs")

    print(f"Applied after {result['waited']}s — stored compose matches and every service "
          f"runs the image it should.")
    # Same as install: if the app has a blueprint, its expectations are what
    # makes "it works" checkable. An update that leaves the app broken should
    # not end in a success message.
    expectations = None
    try:
        if os.path.isfile(core.blueprint_path(args.name, allow_paths=True)):
            expectations = core.load_blueprint(args.name, allow_paths=True).get("expect")
    except core.ConvertError:
        expectations = None
    steps = core.post_install_check(
        args.host, args.name, user, password, ssh_user=args.ssh_user, compose_text=text,
        expectations=expectations, timeout=args.wait_timeout,
        on_progress=lambda w, s: print(f"  t+{w:>4}s  {s}"),
    )
    failed = 0
    for s in steps:
        print(f"  [{'ok  ' if s['ok'] else 'FAIL'}] {s['step']} — {s['detail']}")
        if s["hint"]:
            print(f"         → {s['hint']}")
        failed += 0 if s["ok"] else 1
    if failed:
        die(f"{failed} check(s) failed after the update.")
    print("The app is up and reachable.")


def cmd_blueprints(args):
    """Show what is in the catalogue — including how stale the proof is."""
    entries = core.list_blueprints()
    if not entries:
        print(f"No blueprints in {core.BLUEPRINT_DIR}.")
        return
    for bp in entries:
        v = bp.get("verified") or {}
        proof = (f"verified {v.get('date')} on {v.get('host')}" if v.get("date")
                 else "NEVER VERIFIED")
        print(f"{bp['name']:<18} {bp['category']:<14} {bp['expectations']} expectation(s)  {proof}")
        if bp.get("tagline"):
            print(f"{'':<18} {bp['tagline']}")


def cmd_drift(args):
    """Has upstream moved under the blueprints, and do they still convert?

    Needs the network, not a ZimaOS host — that is what makes it runnable in a
    CI. It therefore says nothing about any installation, and the output says so
    instead of letting "all fine" be read as more than it is.
    """
    names = args.names or [bp["name"] for bp in core.list_blueprints()]
    if not names:
        print("No blueprints to check.")
        return
    results = []
    for name in names:
        try:
            results.append(core.check_blueprint_drift(name))
        except core.ConvertError as e:
            results.append({"name": name, "status": "broken", "notes": [],
                            "problems": [str(e)], "source": None, "pinned": False,
                            "fingerprint": None, "recorded": None})

    mark = {"ok": "ok     ", "moved": "MOVED  ", "broken": "BROKEN ", "unrecorded": "no proof"}
    for r in results:
        print(f"[{mark[r['status']]}] {r['name']}"
              + (f"  ({r['source']}{', pinned' if r['pinned'] else ''})" if r["source"] else ""))
        for note in r["notes"]:
            print(f"    - {note}")
        for problem in r["problems"]:
            print(f"    ERROR: {problem}")
        if r["fingerprint"]:
            print(f"    source_sha256: {r['fingerprint']}")

    broken = [r for r in results if r["status"] == "broken"]
    moved = [r for r in results if r["status"] in ("moved", "unrecorded")]
    print(f"\n{len(results)} blueprint(s): {len(results) - len(broken) - len(moved)} unchanged, "
          f"{len(moved)} moved or unproven, {len(broken)} broken.")
    print("This checked upstream and the conversion. It did NOT look at any installation — "
          "for that: `zimapp.py update <app> --blueprint <name>` (exit 2 on drift) and "
          "`zimapp.py verify <name>`.")
    if broken:
        sys.exit(1)
    if moved:
        sys.exit(2)


def cmd_verify(args):
    """Run a blueprint's expectations against the running installation.

    This is what makes "tested" mean something: not a claim in a README, but
    assertions executed against the live app — on the payload, not just the
    status code.
    """
    blueprint = core.load_blueprint(args.name, allow_paths=True)
    expectations = blueprint.get("expect") or []
    if not expectations:
        die(f"Blueprint '{args.name}' has no expect block — there is nothing to verify.")

    if args.url:
        base, status = args.url, None
    else:
        user, password = credentials(args)
        base, status = core.app_base_url(args.host, blueprint["name"], user, password)
    print(f"Verifying {blueprint['name']} at {base}"
          + (f" (app grid status: {status})" if status else ""))

    results = core.run_expectations(base, expectations)
    failed = 0
    for r in results:
        mark = "ok  " if r["ok"] else "FAIL"
        if not r["ok"]:
            failed += 1
        print(f"  [{mark}] {r['url']} — {r['detail']}")

    if status and status != "running":
        print(f"  [FAIL] app grid reports status '{status}', expected 'running'")
        failed += 1

    if failed:
        die(f"{failed} of {len(results)} expectation(s) failed — this app is NOT verified.")
    print(f"All {len(results)} expectation(s) hold.")


def cmd_serve(args):
    import zimapp_web
    zimapp_web.serve(args.bind, args.port)


def main():
    # Register the shared options twice, so that both
    # "zimapp --host X inspect img" and "zimapp inspect img --host X" work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=os.environ.get("ZIMA_HOST", ""))
    common.add_argument("--ssh-user", default=os.environ.get("ZIMA_SSH_USER", ""))
    common.add_argument("--user")
    common.add_argument("--pass", dest="password")

    p = argparse.ArgumentParser(description="Compose URL or Docker image → ZimaOS app", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True,
                           parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    # The blueprint may only fill values the user did NOT set. argparse cannot
    # tell "not given" from "given, equals the default", so the defaults are
    # kept here for that comparison.
    global PARSER_DEFAULTS
    PARSER_DEFAULTS = {
        "author": "", "category": "Utilities", "tagline": "Self-hosted app",
        "description": None, "icon": "https://icon.casaos.io/main/all/box.png",
        "memory": "2GB", "cpus": "2.00", "name": None, "title": None, "main": None,
    }

    def add_meta(sp):
        sp.add_argument("--name", help="app ID (directory name under /DATA/AppData)")
        sp.add_argument("--title")
        sp.add_argument("--author", default="")
        sp.add_argument("--category", default="Utilities")
        sp.add_argument("--tagline", default="Self-hosted app")
        sp.add_argument("--description", default=None)
        # NOT .../all/default.png — that URL 404s (checked 2026-07-19), which would
        # hand every generated app an empty tile without ZimaOS saying a word
        # (Rule 8). box.png is a generic placeholder that actually resolves.
        sp.add_argument("--icon", default="https://icon.casaos.io/main/all/box.png",
                        help="icon URL — must be reachable, or the tile stays empty (Rule 8)")
        sp.add_argument("--memory", default="2GB", help="period as the decimal separator (Rule 6)")
        sp.add_argument("--cpus", default="2.00")

    sp = sub.add_parser("convert", help="turn a compose/Dockerfile URL into a ZimaOS app")
    sp.add_argument("url", nargs="?", help="URL or path to docker-compose.yml / Dockerfile")
    sp.add_argument("--blueprint", help="use a tested blueprint instead of a bare URL "
                                        "(see: zimapp.py blueprints)")
    add_meta(sp)
    sp.add_argument("--main", help="the service that provides the WebUI (x-casaos.main)")
    sp.add_argument("--image", dest="image_ref", help="image name — mandatory when the source is a Dockerfile")
    sp.add_argument("--port", type=int, help="WebUI container port — needed when the source names none")
    sp.add_argument("--var", action="append", metavar="NAME=VALUE",
                    help="value for a ${VAR} from the source (can be used multiple times)")
    sp.add_argument("--no-secrets", action="store_true",
                    help="do NOT generate missing passwords automatically")
    sp.add_argument("--no-icon-check", action="store_true",
                    help="do not check the icon URL for reachability")
    sp.add_argument("--check-ports", action="store_true",
                    help="read the taken host ports from the target host")
    sp.add_argument("--port-source", choices=["auto", "api", "ssh"], default="auto",
                    help="where to read them from: 'api' = app grid (needs credentials, "
                         "sees only ZimaOS apps), 'ssh' = docker on the host (needs a key, "
                         "sees everything), 'auto' = both, whatever answers (default)")
    sp.add_argument("-o", "--out", help="write to a file instead of stdout")

    sp = sub.add_parser("inspect", help="show EXPOSE/VOLUME/ENV of an image")
    sp.add_argument("image")

    sp = sub.add_parser("generate", help="turn a single Docker image into a ZimaOS app")
    sp.add_argument("image")
    add_meta(sp)
    sp.add_argument("--offline", action="store_true", help="without image inspection (needs --port)")
    sp.add_argument("--port", type=int, help="container port of the WebUI (only needed with --offline)")

    sub.add_parser("validate", help="validate a compose file against the ZimaOS rules").add_argument("file")

    sp = sub.add_parser("install", help="install a compose file on ZimaOS and wait for it to come up")
    sp.add_argument("file")
    sp.add_argument("--no-wait", action="store_true",
                    help="return right after the API accepted it, without the follow-up checks")
    sp.add_argument("--wait-timeout", type=int, default=300,
                    help="how long to wait for the app to report 'running' (seconds, default 300)")
    sub.add_parser("uninstall", help="remove an app including its data directory").add_argument("name")

    sp = sub.add_parser("update", help="change an installed app in place (read back, diff, apply)")
    sp.add_argument("name", help="the installed app")
    sp.add_argument("--source", help="URL of the compose/Dockerfile to re-convert from")
    sp.add_argument("--blueprint", help="use a blueprint's pinned source instead")
    sp.add_argument("--file", help="a finished compose file instead of a re-conversion")
    sp.add_argument("--apply", action="store_true",
                    help="really apply it — without this the command only shows the diff "
                         "and exits with code 2 if there is one")
    sp.add_argument("--var", action="append", metavar="NAME=VALUE",
                    help="override a value that would otherwise be kept from the installation")
    sp.add_argument("--keep", action="append", metavar="NAME",
                    help="keep the installed value of this environment key, whatever the new "
                         "definition says (generated secrets are kept anyway)")
    sp.add_argument("--title")
    sp.add_argument("--author")
    sp.add_argument("--category")
    sp.add_argument("--tagline")
    sp.add_argument("--description")
    sp.add_argument("--icon")
    sp.add_argument("--memory")
    sp.add_argument("--cpus")
    sp.add_argument("--main")
    sp.add_argument("--no-icon-check", action="store_true",
                    help="do not check the icon URL for reachability")
    sp.add_argument("--wait-timeout", type=int, default=180,
                    help="how long to wait for the change to actually arrive (seconds, default 180)")
    sp.add_argument("-o", "--out", help="also write the new compose to a file")

    sub.add_parser("blueprints", help="list the tested blueprints")

    sp = sub.add_parser("drift", help="has upstream moved under the blueprints? (no ZimaOS host needed)")
    sp.add_argument("names", nargs="*", help="blueprints to check (default: all)")

    sp = sub.add_parser("verify", help="run a blueprint's expectations against the live install")
    sp.add_argument("name", help="blueprint / app name")
    sp.add_argument("--url", help="base URL of the installation (default: looked up in the app grid)")

    sp = sub.add_parser("serve", help="start the web UI")
    sp.add_argument("--bind", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8790)

    args = p.parse_args()
    if args.cmd == "convert" and not args.url and not args.blueprint:
        p.error("convert needs a URL or --blueprint")
    # "auto" means: try every source and use whatever answers.
    source = getattr(args, "port_source", "auto")
    args.port_source_list = ("api", "ssh") if source == "auto" else (source,)
    handler = {"convert": cmd_convert, "inspect": cmd_inspect, "generate": cmd_generate,
               "validate": cmd_validate, "install": cmd_install, "uninstall": cmd_uninstall,
               "update": cmd_update, "blueprints": cmd_blueprints, "verify": cmd_verify,
               "drift": cmd_drift,
               "serve": cmd_serve}[args.cmd]
    try:
        handler(args)
    except ConvertError as e:
        die(str(e))


if __name__ == "__main__":
    main()
