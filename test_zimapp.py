#!/usr/bin/env python3
"""
Regression tests for the converter — they run without a network and without a ZimaOS host.

Run with:  python3 -m unittest -v test_zimapp

Every test pins down exactly one rule that has already bitten us once.
Whoever changes a rule has to come by here.
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest

import yaml

import zimapp_core as core

META = {"name": "demo", "title": "Demo", "category": "Utilities",
        "icon": "https://example.invalid/icon.png", "tagline": "Test"}


def build(compose_text, meta=None, options=None):
    doc = yaml.safe_load(textwrap.dedent(compose_text))
    result, info = core.convert(doc, dict(META, **(meta or {})), options or {})
    return result, info, core.dump(result)


class ComposeConversion(unittest.TestCase):

    def test_port_map_is_a_string(self):
        """Rule 5: an int breaks the ZimaOS parser, the tile vanishes wordlessly."""
        result, _, text = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
        """)
        self.assertIsInstance(result["x-casaos"]["port_map"], str)
        self.assertIn("port_map: '8080'", text)

    def test_published_is_a_string(self):
        _, _, text = build("""
            services:
              app: {image: nginx, ports: ["8080:80"]}
        """)
        self.assertIn("published: '8080'", text)

    def test_reserved_port_is_moved(self):
        """80 belongs to ZimaOS itself — otherwise the app collides with the gateway."""
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["80:80"]}
        """)
        self.assertNotEqual(result["x-casaos"]["port_map"], "80")

    def test_taken_host_port_is_moved(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"]}
        """, options={"used_ports": {8080, 8081}})
        self.assertEqual(result["x-casaos"]["port_map"], "8082")

    def test_decimal_separator_is_a_period(self):
        """Rule 6: '14,92GB' → strconv.ParseFloat → HTTP 400."""
        _, _, text = build("""
            services:
              app: {image: nginx, ports: ["8080:80"]}
        """, meta={"memory": "14,92GB", "cpus": "2,00"})
        self.assertIn("memory: 14.92GB", text)
        self.assertNotIn(",", text.split("deploy:")[1].split("x-casaos:")[0])

    def test_named_volume_becomes_bind_under_data(self):
        """Rule 7: the ZimaOS redirections only take effect under /DATA/AppData."""
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["pgdata:/var/lib/data"]}
            volumes:
              pgdata:
        """)
        vol = result["services"]["app"]["volumes"][0]
        self.assertEqual(vol["type"], "bind")
        self.assertEqual(vol["source"], "/DATA/AppData/demo/pgdata")
        self.assertNotIn("volumes", result)   # top-level volumes block is dropped

    def test_relative_bind_is_rewritten(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["./data:/data"]}
        """)
        self.assertEqual(result["services"]["app"]["volumes"][0]["source"],
                         "/DATA/AppData/demo/data")

    def test_socket_is_left_untouched(self):
        """A rewritten Docker socket would be broken, not a data directory."""
        result, _, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
        """)
        vol = result["services"]["app"]["volumes"][0]
        self.assertEqual(vol["source"], "/var/run/docker.sock")
        self.assertTrue(vol["read_only"])

    def test_main_is_not_the_database(self):
        """x-casaos.main has to point at the WebUI, not at Postgres."""
        result, info, _ = build("""
            services:
              db: {image: postgres:16, ports: ["5432:5432"]}
              web: {image: myapp, ports: ["8080:80"]}
        """)
        self.assertEqual(info["main"], "web")
        self.assertEqual(result["x-casaos"]["main"], "web")

    def test_app_id_comes_from_the_main_service(self):
        """The first service is often the broker — then /DATA/AppData would be named wrongly."""
        doc = yaml.safe_load(textwrap.dedent("""
            services:
              broker: {image: redis:8}
              webserver: {image: ghcr.io/paperless-ngx/paperless-ngx, ports: ["8000:8000"]}
        """))
        result, _ = core.convert(doc, {"category": "Documents"}, {})
        self.assertEqual(result["name"], "paperless-ngx")

    def test_all_services_share_one_network(self):
        """Without a shared network the app does not find its database via DNS (§3.4)."""
        result, _, _ = build("""
            services:
              db: {image: postgres:16}
              web: {image: myapp, ports: ["8080:80"]}
        """)
        self.assertEqual(list(result["networks"]), ["demo-network"])
        for svc in result["services"].values():
            self.assertEqual(svc["networks"], ["demo-network"])

    def test_build_without_image_is_an_error(self):
        """ZimaOS does not build images (§4.4.1) — that must not slip through silently."""
        with self.assertRaises(core.ConvertError):
            build("""
                services:
                  app: {build: ., ports: ["8080:80"]}
            """)

    def test_service_without_a_port_is_an_error(self):
        with self.assertRaises(core.ConvertError):
            build("""
                services:
                  db: {image: postgres:16}
            """)

    def test_env_file_is_resolved(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], env_file: .env}
        """, options={"env_defaults": {"FOO": "bar"}, "env_source": "http://x/.env"})
        self.assertNotIn("env_file", result["services"]["app"])
        self.assertIn("FOO=bar", result["services"]["app"]["environment"])

    def test_env_file_without_values_is_reported(self):
        result, info, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], env_file: .env}
        """)
        self.assertNotIn("env_file", result["services"]["app"])
        self.assertTrue(any("env_file" in w for w in info["warnings"]))

    def test_tz_is_added_but_not_overwritten(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], environment: ["TZ=UTC"]}
              db: {image: postgres:16}
        """)
        self.assertIn("TZ=UTC", result["services"]["app"]["environment"])
        self.assertIn("TZ=Europe/Berlin", result["services"]["db"]["environment"])


class Variables(unittest.TestCase):

    def test_default_is_used(self):
        text, generated = core.resolve_variables("a: ${FOO:-bar}", {})
        self.assertEqual(text, "a: bar")
        self.assertEqual(generated, {})

    def test_user_value_beats_the_default(self):
        text, _ = core.resolve_variables("a: ${FOO:-bar}", {"FOO": "baz"})
        self.assertEqual(text, "a: baz")

    def test_secret_is_generated_and_reported(self):
        text, generated = core.resolve_variables("p: ${DB_PASSWORD}", {})
        self.assertIn("DB_PASSWORD", generated)
        self.assertNotIn("${", text)

    def test_same_variable_gets_the_same_value(self):
        """Otherwise the app and the database know different passwords."""
        text, _ = core.resolve_variables("a: ${DB_PASSWORD}\nb: ${DB_PASSWORD}", {})
        a, b = [line.split(": ", 1)[1] for line in text.splitlines()]
        self.assertEqual(a, b)

    def test_without_autofill_it_stays_unresolved(self):
        text, generated = core.resolve_variables("p: ${DB_PASSWORD}", {}, autofill_secrets=False)
        self.assertIn("${DB_PASSWORD}", text)
        self.assertEqual(generated, {})

    def test_dotenv_parser(self):
        values = core.parse_dotenv("# comment\nexport A=1\nB=\"two\"\nbroken\n")
        self.assertEqual(values, {"A": "1", "B": "two"})


class Validator(unittest.TestCase):

    def _minimal(self, **over):
        doc = {
            "name": "demo",
            "services": {"app": {"image": "nginx", "ports": [
                {"mode": "ingress", "target": 80, "published": "8080", "protocol": "tcp"}]}},
            "x-casaos": {"main": "app", "port_map": "8080", "icon": "http://x/i.png",
                         "title": {"en_us": "Demo"}, "description": {"en_us": "Demo"},
                         "category": "Utilities"},
        }
        doc.update(over)
        return core.dump(doc)

    def test_clean_compose_has_no_problems(self):
        problems, _ = core.validate(self._minimal())
        self.assertEqual(problems, [])

    def test_int_port_map_is_detected(self):
        text = self._minimal().replace("port_map: '8080'", "port_map: 8080")
        problems, _ = core.validate(text)
        self.assertTrue(any("port_map" in p for p in problems))

    def test_port_map_without_a_matching_port_is_detected(self):
        text = self._minimal().replace("port_map: '8080'", "port_map: '9999'")
        problems, _ = core.validate(text)
        self.assertTrue(any("9999" in p for p in problems))

    def test_comma_in_memory_is_detected(self):
        text = self._minimal().replace(
            "ports:", "deploy:\n      resources:\n        limits:\n          memory: 14,92GB\n    ports:")
        problems, _ = core.validate(text)
        self.assertTrue(any("comma" in p for p in problems))

    def test_unresolved_variable_is_a_blocker(self):
        problems, _ = core.validate(self._minimal().replace("nginx", "nginx:${TAG}"))
        self.assertTrue(any("Unresolved variables" in p for p in problems))

    def test_main_must_exist(self):
        text = self._minimal().replace("main: app", "main: doesnotexist")
        problems, _ = core.validate(text)
        self.assertTrue(any("doesnotexist" in p for p in problems))

    def test_env_file_is_a_blocker(self):
        text = self._minimal().replace("image: nginx", "image: nginx\n    env_file: .env")
        problems, _ = core.validate(text)
        self.assertTrue(any("env_file" in p for p in problems))

    def test_duplicate_host_port_is_detected(self):
        doc = yaml.safe_load(self._minimal())
        doc["services"]["b"] = {"image": "x", "ports": [
            {"mode": "ingress", "target": 90, "published": "8080", "protocol": "tcp"}]}
        problems, _ = core.validate(core.dump(doc))
        self.assertTrue(any("8080" in p for p in problems))

    def test_broken_yaml_is_the_first_error(self):
        problems, _ = core.validate("name: [unclosed\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("not parsable", problems[0])


class PortSources(unittest.TestCase):
    """Port collection from the app grid API and from docker-over-SSH.

    Both sources are stubbed — the point is the combination logic and, above
    all, that a failing source becomes a visible note instead of silence.
    """

    GRID = json.dumps({"data": [
        {"name": "uptime-kuma", "port": "3001", "status": "running"},
        {"name": "paperless-ngx", "port": "8000", "status": "running"},
        {"name": "broken", "port": "", "status": "failed"},      # must not crash
        {"name": "noport", "status": "running"},                 # key missing entirely
    ]})

    def setUp(self):
        self._api, self._login, self._ssh = core.api, core.login, core.used_host_ports
        core.login = lambda h, u, p: "token"
        core.api = lambda *a, **kw: (200, self.GRID)

    def tearDown(self):
        core.api, core.login, core.used_host_ports = self._api, self._login, self._ssh

    def test_api_reads_app_ports(self):
        self.assertEqual(core.used_app_ports("h", "u", "p"), {3001, 8000})

    def test_api_rejects_non_200(self):
        core.api = lambda *a, **kw: (401, "nope")
        with self.assertRaises(core.ConvertError):
            core.used_app_ports("h", "u", "p")

    def test_both_sources_are_merged(self):
        core.used_host_ports = lambda host, ssh_user: {8000, 9000}
        ports, notes = core.collect_used_ports("h", ssh_user="zima", user="u", password="p")
        self.assertEqual(ports, {3001, 8000, 9000})
        self.assertEqual(len(notes), 2)

    def test_api_alone_is_enough(self):
        """Inside a container there is no SSH key — the API has to carry it."""
        def no_ssh(host, ssh_user):
            raise core.ConvertError("permission denied")
        core.used_host_ports = no_ssh
        ports, notes = core.collect_used_ports("h", ssh_user="zima", user="u", password="p")
        self.assertEqual(ports, {3001, 8000})
        self.assertTrue(any("unusable" in n for n in notes))

    def test_missing_credentials_are_named(self):
        core.used_host_ports = lambda host, ssh_user: {9000}
        ports, notes = core.collect_used_ports("h", ssh_user="zima")
        self.assertEqual(ports, {9000})
        self.assertTrue(any("no credentials" in n for n in notes))

    def test_no_working_source_is_an_error(self):
        """Never return an empty set quietly — that would look like 'nothing taken'."""
        def no_ssh(host, ssh_user):
            raise core.ConvertError("permission denied")
        core.used_host_ports = no_ssh
        with self.assertRaises(core.ConvertError) as ctx:
            core.collect_used_ports("h", ssh_user="zima")
        self.assertIn("no source worked", str(ctx.exception))

    def test_missing_ssh_binary_degrades(self):
        """Inside the container there is no ssh binary at all.

        FileNotFoundError is not a ConvertError, so without translation it
        escapes collect_used_ports and kills the whole request with a 500 —
        which is exactly what happened live before this was fixed.
        """
        core.used_host_ports = self._ssh                       # real implementation
        real_run = core.subprocess.run

        def no_ssh_binary(*a, **kw):
            raise FileNotFoundError(2, "No such file or directory: 'ssh'")
        core.subprocess.run = no_ssh_binary
        try:
            ports, notes = core.collect_used_ports("h", ssh_user="zima", user="u", password="p")
        finally:
            core.subprocess.run = real_run
        self.assertEqual(ports, {3001, 8000})                  # API carried it
        self.assertTrue(any("no 'ssh' client" in n for n in notes))

    def test_source_selection_is_respected(self):
        core.used_host_ports = lambda host, ssh_user: {9000}
        ports, _ = core.collect_used_ports("h", ssh_user="zima", user="u", password="p",
                                           sources=("api",))
        self.assertEqual(ports, {3001, 8000})

    def test_taken_port_is_avoided_end_to_end(self):
        """The whole point: a port the grid reports must not be proposed again."""
        doc = yaml.safe_load("services:\n  app: {image: x, ports: ['8000:80']}\n")
        result, _ = core.convert(doc, dict(META), {"used_ports": core.used_app_ports("h", "u", "p")})
        self.assertNotEqual(result["x-casaos"]["port_map"], "8000")


class Blueprints(unittest.TestCase):
    """The catalogue itself is data — a broken blueprint must be caught here."""

    def test_shipped_blueprints_load(self):
        entries = core.list_blueprints()
        self.assertTrue(entries, "no blueprints found — the catalogue is empty")
        for entry in entries:
            bp = core.load_blueprint(entry["name"])
            self.assertTrue(bp["source"].startswith("http"), bp["name"])
            self.assertTrue(bp.get("expect"),
                            f"{bp['name']} has no expect block — then 'tested' means nothing")

    def test_verified_block_uses_host_not_on(self):
        """`on:` in YAML 1.1 is the boolean True, so verified.on is unreadable by name."""
        for entry in core.list_blueprints():
            bp = core.load_blueprint(entry["name"])
            verified = bp.get("verified") or {}
            self.assertNotIn(True, verified.keys(),
                             f"{bp['name']}: use 'host:' instead of 'on:' in the verified block")
            if verified:
                self.assertIn("host", verified)

    def test_listing_is_json_serialisable(self):
        """An unquoted YAML date becomes datetime.date — json chokes on it.

        That crashed /api/defaults with a 500 and no body at all, so the UI
        could not even show the catalogue.
        """
        json.dumps(core.list_blueprints())

    def test_missing_field_is_an_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write("name: broken\n")            # source missing
        with self.assertRaises(core.ConvertError):
            core.load_blueprint(fh.name)

    def test_unknown_blueprint_names_the_alternatives(self):
        with self.assertRaises(core.ConvertError) as ctx:
            core.load_blueprint("does-not-exist")
        self.assertIn("Available", str(ctx.exception))

    def test_pin_rewrites_the_raw_url(self):
        bp = {"source": "https://raw.githubusercontent.com/o/r/main/d/c.yml", "pin": "abc123"}
        url, pinned = core.blueprint_source_url(bp)
        self.assertTrue(pinned)
        self.assertIn("/abc123/", url)
        self.assertNotIn("/main/", url)

    def test_without_pin_the_url_stays(self):
        bp = {"source": "https://example.invalid/c.yml"}
        url, pinned = core.blueprint_source_url(bp)
        self.assertFalse(pinned)
        self.assertEqual(url, bp["source"])

    def test_env_placeholders_use_the_assigned_port(self):
        """The whole reason env is applied after convert(): the port is only known then."""
        doc = {"services": {"web": {"environment": ["TZ=Europe/Berlin"]}}}
        bp = {"name": "t", "env": {"web": {"URL": "${scheme}://${host}:${port}",
                                          "KEY": "${generate:16}"}}}
        generated = core.apply_blueprint_env(doc, bp, "10.0.0.5", "8001", "t")
        env = doc["services"]["web"]["environment"]
        self.assertIn("URL=http://10.0.0.5:8001", env)
        self.assertIn("KEY", generated)
        self.assertTrue(any(e.startswith("KEY=") and len(e) > 12 for e in env))

    def test_env_overrides_an_existing_value(self):
        doc = {"services": {"web": {"environment": ["TZ=UTC", "A=old"]}}}
        core.apply_blueprint_env(doc, {"name": "t", "env": {"web": {"A": "new"}}}, "h", "1", "t")
        self.assertIn("A=new", doc["services"]["web"]["environment"])
        self.assertNotIn("A=old", doc["services"]["web"]["environment"])

    def test_renamed_upstream_service_is_an_error(self):
        """If upstream renames the service, the blueprint must fail loudly, not quietly skip."""
        doc = {"services": {"webserver": {}}}
        with self.assertRaises(core.ConvertError) as ctx:
            core.apply_blueprint_env(doc, {"name": "t", "env": {"web": {"A": "1"}}}, "h", "1", "t")
        self.assertIn("does not exist", str(ctx.exception))


class Expectations(unittest.TestCase):
    """Expectations run against a real server — a stub would not prove much."""

    @classmethod
    def setUpClass(cls):
        import threading

        import zimapp_web
        cls.httpd = zimapp_web.ThreadingHTTPServer(("127.0.0.1", 0), zimapp_web.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_status_and_payload_hold(self):
        results = core.run_expectations(self.base, [
            {"http": "/icon.svg", "status": 200, "contains": "<svg", "min_bytes": 200},
        ])
        self.assertTrue(results[0]["ok"], results[0]["detail"])

    def test_status_alone_is_not_enough(self):
        """A 200 with the wrong payload must fail — that is the entire point."""
        results = core.run_expectations(self.base, [
            {"http": "/icon.svg", "status": 200, "contains": "this is not in there"},
        ])
        self.assertFalse(results[0]["ok"])
        self.assertIn("missing in body", results[0]["detail"])

    def test_absent_catches_error_text(self):
        results = core.run_expectations(self.base, [
            {"http": "/icon.svg", "absent": "svg"},
        ])
        self.assertFalse(results[0]["ok"])
        self.assertIn("must not appear", results[0]["detail"])

    def test_missing_page_fails(self):
        results = core.run_expectations(self.base, [{"http": "/nope", "status": 200}])
        self.assertFalse(results[0]["ok"])

    def test_unreachable_host_fails_without_raising(self):
        results = core.run_expectations("http://127.0.0.1:9", [{"http": "/", "status": 200}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("not reachable", results[0]["detail"])

    def test_min_bytes_catches_an_empty_body(self):
        results = core.run_expectations(self.base, [{"http": "/icon.svg", "min_bytes": 999999}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("bytes", results[0]["detail"])


class PostInstall(unittest.TestCase):
    """The follow-up between "accepted" and "actually usable".

    The grid and SSH are stubbed; what is under test is the sequencing and —
    above all — that a failure names its cause instead of just saying "failed".
    """

    def setUp(self):
        self._login, self._api, self._ssh_docker = core.login, core.api, core.ssh_docker
        self._sleep = core.time.sleep
        core.login = lambda h, u, p: "token"
        core.time.sleep = lambda s: None                  # no real waiting in tests

    def tearDown(self):
        core.login, core.api, core.ssh_docker = self._login, self._api, self._ssh_docker
        core.time.sleep = self._sleep

    def _grid(self, *states):
        """Return a different grid on each call, so a transition can be tested."""
        seq = list(states)

        def fake(*a, **kw):
            state = seq.pop(0) if len(seq) > 1 else seq[0]
            data = [] if state is None else [{"name": "app", "status": state, "port": "8080"}]
            return 200, json.dumps({"data": data})
        return fake

    def test_waits_until_running(self):
        core.api = self._grid(None, "installing", "running")
        ok, status, port, waited = core.wait_for_app("h", "app", "u", "p", timeout=30, interval=1)
        self.assertTrue(ok)
        self.assertEqual((status, port), ("running", "8080"))

    def test_timeout_does_not_raise(self):
        core.api = self._grid(None)
        ok, status, port, waited = core.wait_for_app("h", "app", "u", "p", timeout=5, interval=1)
        self.assertFalse(ok)
        self.assertIsNone(status)

    def test_missing_image_is_named_as_the_cause(self):
        """The real failure from 2026-07-19: uninstall deleted the locally built image."""
        core.api = self._grid(None)
        core.ssh_docker = lambda host, user, args: "other:latest\n"
        steps = core.post_install_check(
            "h", "app", "u", "p", ssh_user="zima", timeout=3,
            compose_text="services:\n  a:\n    image: zimapp:local\n")
        self.assertFalse(steps[0]["ok"])
        self.assertIn("zimapp:local", steps[0]["hint"])
        self.assertIn("deletes", steps[0]["hint"])

    def test_present_image_is_not_blamed(self):
        core.api = self._grid(None)
        core.ssh_docker = lambda host, user, args: "zimapp:local\n"
        steps = core.post_install_check(
            "h", "app", "u", "p", ssh_user="zima", timeout=3,
            compose_text="services:\n  a:\n    image: zimapp:local\n")
        self.assertIn("all images are present", steps[0]["hint"])

    def _with_probe(self, reachable, zfw):
        """Stub the probe and the firewall check — no real port may decide this."""
        probe, zfw_fn = core.http_probe, core.zfw_active
        core.http_probe = lambda url, timeout=10: (reachable, "stub")
        core.zfw_active = lambda host, ssh_user: zfw
        self.addCleanup(lambda: (setattr(core, "http_probe", probe),
                                 setattr(core, "zfw_active", zfw_fn)))

    def test_unreachable_port_with_active_zfw_names_apply_and_commit(self):
        core.api = self._grid("running")
        self._with_probe(reachable=False, zfw=True)
        steps = core.post_install_check("h", "app", "u", "p", ssh_user="zima", timeout=3)
        self.assertFalse(steps[1]["ok"])
        self.assertIn("zfw apply", steps[1]["hint"])
        self.assertIn("commit", steps[1]["hint"])

    def test_unreachable_port_without_zfw_blames_the_container(self):
        """Wrong advice is worse than none: no ZFW means it is not the firewall."""
        core.api = self._grid("running")
        self._with_probe(reachable=False, zfw=False)
        steps = core.post_install_check("h", "app", "u", "p", ssh_user="zima", timeout=3)
        self.assertIn("not active", steps[1]["hint"])
        self.assertIn("docker logs", steps[1]["hint"])

    def test_unknown_firewall_state_is_admitted(self):
        core.api = self._grid("running")
        self._with_probe(reachable=False, zfw=None)
        steps = core.post_install_check("h", "app", "u", "p", timeout=3)
        self.assertIn("could not determine", steps[1]["hint"])

    def test_reachable_app_passes(self):
        core.api = self._grid("running")
        self._with_probe(reachable=True, zfw=None)
        steps = core.post_install_check("h", "app", "u", "p", timeout=3)
        self.assertTrue(all(s["ok"] for s in steps))

    def test_http_probe_counts_any_answer_as_reachable(self):
        """401/302 mean the app is up and doing its job — only silence is a failure."""
        ok, detail = core.http_probe("http://127.0.0.1:9")
        self.assertFalse(ok)

    def test_missing_images_without_ssh_returns_none(self):
        """No SSH means 'cannot tell' — which must not be reported as 'nothing missing'."""
        self.assertIsNone(core.missing_images("h", None, "services: {}"))


class IconCheck(unittest.TestCase):
    """Against our own server, without network access to the outside."""

    @classmethod
    def setUpClass(cls):
        import threading

        import zimapp_web
        cls.httpd = zimapp_web.ThreadingHTTPServer(("127.0.0.1", 0), zimapp_web.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_reachable_icon(self):
        self.assertIsNone(core.check_icon(self.base + "/icon.svg"))

    def test_head_is_answered(self):
        """Without do_HEAD, BaseHTTPRequestHandler answers 501 — a healthy icon would look dead."""
        import urllib.request
        req = urllib.request.Request(self.base + "/icon.svg", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")            # HEAD: headers yes, body no
            self.assertEqual(resp.headers["Content-Type"], "image/svg+xml")

    def test_missing_icon_is_reported(self):
        self.assertIn("404", core.check_icon(self.base + "/doesnotexist.png"))

    def test_empty_and_broken_icon(self):
        self.assertIn("no icon", core.check_icon(""))
        self.assertIn("not an http", core.check_icon("data:image/png;base64,xxx"))


class BlueprintValuesReachTheOutput(unittest.TestCase):
    """A recipe's memory/cpus/tagline must not be overridden by form defaults.

    The form shipped hard-coded value= defaults, so `if blueprint.get(k) and not
    meta.get(k)` was never true and the recipe was silently ignored (review
    2026-07-21). The fields are placeholders now, and an empty value must not be
    mistaken for a set one.
    """

    COMPOSE = 'services:\n  app:\n    image: nginx\n    ports: ["8080:80"]\n'

    def test_empty_strings_do_not_win_over_the_default(self):
        result, _, _ = build(self.COMPOSE, meta={"memory": "", "cpus": "",
                                                 "tagline": "", "index": ""})
        limits = result["services"]["app"]["deploy"]["resources"]["limits"]
        self.assertEqual(limits["memory"], "2GB")
        self.assertEqual(limits["cpus"], "2.00")
        self.assertEqual(result["x-casaos"]["tagline"]["en_us"], "Self-hosted app")
        self.assertEqual(result["x-casaos"]["index"], "/")

    def test_a_set_value_still_wins(self):
        result, _, _ = build(self.COMPOSE, meta={"memory": "4GB", "cpus": "4.00"})
        limits = result["services"]["app"]["deploy"]["resources"]["limits"]
        self.assertEqual(limits["memory"], "4GB")
        self.assertEqual(limits["cpus"], "4.00")

    def test_the_listing_carries_the_fields_the_form_needs(self):
        bp = [b for b in core.list_blueprints() if b["name"] == "paperless-ngx"][0]
        for key in ("memory", "icon", "main"):
            self.assertIn(key, bp)

    def test_the_listing_never_carries_vars(self):
        """It is served unauthenticated and a saved blueprint may hold a password."""
        for bp in core.list_blueprints():
            self.assertNotIn("vars", bp)
            self.assertNotIn("env", bp)


class OneSourceOnly(unittest.TestCase):
    """The server refuses contradicting sources instead of picking a winner.

    The UI kept file/URL/blueprint mutually exclusive — except a blueprint
    selection writes the URL field programmatically, which fires no `input` event,
    so the clearing never ran. The generated compose then took its YAML from the
    file and its env_file from the URL's directory: a foreign project's passwords,
    silently (review 2026-07-21).
    """

    COMPOSE = 'services:\n  app:\n    image: nginx\n    ports: ["8080:80"]\n'

    def setUp(self):
        import zimapp_web
        self.web = zimapp_web

    def test_file_plus_url_is_refused(self):
        with self.assertRaises(core.ConvertError) as cm:
            self.web.api_analyze({"text": self.COMPOSE, "url": "https://example.invalid/c.yml"})
        self.assertIn("Pick one source", str(cm.exception))
        self.assertIn("Nothing was converted", str(cm.exception))

    def test_file_plus_blueprint_is_refused(self):
        with self.assertRaises(core.ConvertError) as cm:
            self.web.api_generate({"text": self.COMPOSE, "blueprint": "paperless-ngx"})
        self.assertIn("Pick one source", str(cm.exception))

    def test_a_file_alone_is_fine(self):
        result = self.web.api_analyze({"text": self.COMPOSE, "filename": "x.yml"})
        self.assertEqual(result["kind"], "compose")

    def test_blueprint_with_its_own_url_still_works(self):
        """The UI echoes the blueprint source into the URL field — that must pass."""
        bp = core.load_blueprint("paperless-ngx")
        url = core.blueprint_source_url(bp)[0]
        self.web._one_source_only({"url": url, "blueprint": "paperless-ngx"})   # no raise

    def test_the_two_size_limits_do_not_contradict_each_other(self):
        """The UI advertised 2 MB while the server applied 2 MB to the JSON body,
        which is larger than the file — a 1.9 MB file was accepted and then 413'd."""
        import re as _re
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "static", "app.js"), encoding="utf-8") as fh:
            js = fh.read()
        m = _re.search(r"MAX_OPEN_FILE\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", js)
        self.assertTrue(m, "MAX_OPEN_FILE not found in app.js")
        self.assertLess(int(m.group(1)) * 1024 * 1024, self.web.MAX_BODY,
                        "the file the UI accepts must fit into the request body")

    def test_blueprint_with_a_different_url_is_refused(self):
        with self.assertRaises(core.ConvertError) as cm:
            self.web.api_generate({"url": "https://example.invalid/other.yml",
                                   "blueprint": "paperless-ngx", "meta": {}})
        self.assertIn("Clear one of the two", str(cm.exception))


class BlueprintNameIsNotAPath(unittest.TestCase):
    """A blueprint name arriving over HTTP must never be usable as a file path.

    Found by review 2026-07-21: the container serves on 0.0.0.0 without any
    authentication, and blueprint_path() returned anything containing a separator
    verbatim — so POST /api/verify {"name": "/etc/shadow"} opened that file, and
    the YAML parse error echoed its content back line by line.
    """

    ATTEMPTS = ["/etc/passwd", "../../etc/passwd", "blueprints/../../../etc/passwd",
                "/DATA/AppData/immich/docker-compose.yml", "sub/dir/x.yml", "x.yml"]

    def test_paths_are_refused_by_default(self):
        for name in self.ATTEMPTS:
            with self.subTest(name=name):
                with self.assertRaises(core.ConvertError) as cm:
                    core.load_blueprint(name)
                self.assertIn("name, not a path", str(cm.exception))

    def test_a_plain_name_still_resolves(self):
        bp = core.load_blueprint("paperless-ngx")
        self.assertEqual(bp["name"], "paperless-ngx")

    def test_the_local_cli_may_still_pass_a_path(self):
        """`zimapp convert --blueprint ./my.yml` runs in the user's own shell."""
        path = os.path.join(core.BLUEPRINT_DIR, "paperless-ngx.yml")
        bp = core.load_blueprint(path, allow_paths=True)
        self.assertEqual(bp["name"], "paperless-ngx")

    def test_the_error_does_not_echo_file_content(self):
        """The leak was the parser error quoting the file — a refusal must not."""
        with self.assertRaises(core.ConvertError) as cm:
            core.load_blueprint("/etc/hostname")
        self.assertNotIn("not parsable", str(cm.exception))


class OpenedFile(unittest.TestCase):
    """A .yml opened in the browser: the content arrives, there is no URL to fetch.

    The web UI used to claim "a local file path works too" — it does not: the path
    is resolved inside the container, where the user's file does not exist.
    """

    COMPOSE = textwrap.dedent("""
        services:
          app:
            image: nginx
            ports: ["8080:80"]
    """)

    def test_text_is_used_instead_of_fetching(self):
        text, info = core.build_from_source(
            None, {"name": "demo", "title": "Demo", "main": "app"}, {},
            {"source_text": self.COMPOSE, "source_name": "my.yml", "check_icon": False})
        self.assertIn("image: nginx", text)
        self.assertEqual(info["source"], "my.yml")

    def test_an_empty_file_is_an_error(self):
        with self.assertRaises(core.ConvertError) as cm:
            core.build_from_source(None, {"name": "demo"}, {},
                                   {"source_text": "   \n", "check_icon": False})
        self.assertIn("empty", str(cm.exception))

    def test_env_values_without_env_file_are_not_claimed_as_applied(self):
        """convert() only writes env_defaults where a service declares env_file.

        The analyze note said "N values land directly in 'environment'" even when
        no service had env_file — none of them did (review 2026-07-21).
        """
        import zimapp_web
        real = core.fetch_env_files
        core.fetch_env_files = lambda src, names: ({"A": "1", "B": "2"}, "http://x/example.env")
        try:
            result = zimapp_web.api_analyze({"text": 'services:\n  app:\n'
                                                     '    image: nginx:${TAG:-1}\n'
                                                     '    ports: ["8080:80"]\n'})
        finally:
            core.fetch_env_files = real
        note = " ".join(result["warnings"])
        self.assertIn("do NOT go into", note)
        self.assertNotIn("land directly in 'environment'", note)

    def test_env_file_note_says_nothing_was_searched(self):
        """'could not be found next to it' would imply we looked. There is no next to."""
        compose = self.COMPOSE.replace('ports: ["8080:80"]',
                                       'ports: ["8080:80"]\n    env_file: .env')
        _, info = core.build_from_source(
            None, {"name": "demo", "title": "Demo", "main": "app"}, {},
            {"source_text": compose, "source_name": "my.yml", "check_icon": False})
        note = [w for w in info["warnings"] if "no directory to look next to" in w]
        self.assertTrue(note, info["warnings"])
        self.assertIn("nothing was searched", note[0])

    def test_a_broken_file_is_diagnosed_as_broken_yaml(self):
        with self.assertRaises(core.ConvertError) as cm:
            core.build_from_source(None, {"name": "demo"}, {},
                                   {"source_text": 'services:\n  a:\n\t  b: 1\n',
                                    "check_icon": False})
        self.assertIn("does not parse", str(cm.exception))


class SaveBlueprint(unittest.TestCase):
    """Saving the form as a blueprint — delta + source, never a compose copy."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.real = core.USER_BLUEPRINT_DIR
        core.USER_BLUEPRINT_DIR = self.tmp.name

    def tearDown(self):
        core.USER_BLUEPRINT_DIR = self.real
        self.tmp.cleanup()

    DATA = {"name": "my-app", "title": "My App", "category": "Utilities",
            "source": "https://example.invalid/docker-compose.yml",
            "vars": {"DB_PASSWORD": "hunter2", "TZ": "Europe/Berlin"}}

    def test_it_writes_the_delta_not_the_compose(self):
        path, _ = core.save_blueprint(dict(self.DATA))
        with open(path, encoding="utf-8") as fh:
            bp = yaml.safe_load(fh)
        self.assertEqual(bp["source"], self.DATA["source"])
        self.assertEqual(bp["vars"]["DB_PASSWORD"], "hunter2")
        self.assertNotIn("services", bp)

    def test_it_does_not_claim_to_be_verified(self):
        """`verified:` records what was observed. Nothing was observed here."""
        path, warnings = core.save_blueprint(dict(self.DATA))
        with open(path, encoding="utf-8") as fh:
            bp = yaml.safe_load(fh)
        self.assertNotIn("verified", bp)
        self.assertIn("not a verified recipe", " ".join(warnings))

    def test_secrets_are_named_out_loud(self):
        _, warnings = core.save_blueprint(dict(self.DATA))
        joined = " ".join(warnings)
        self.assertIn("DB_PASSWORD", joined)
        self.assertNotIn("TZ,", joined)                  # not everything is a secret

    def test_file_mode_is_660_not_600(self):
        """0600 would lock the user out: the app writes under its own uid, not theirs."""
        import stat
        path, _ = core.save_blueprint(dict(self.DATA))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o660)

    def test_no_overwrite_without_being_asked(self):
        core.save_blueprint(dict(self.DATA))
        with self.assertRaises(core.ConvertError) as cm:
            core.save_blueprint(dict(self.DATA))
        self.assertIn("already exists", str(cm.exception))
        core.save_blueprint(dict(self.DATA), overwrite=True)     # explicit is fine

    def test_a_shipped_blueprint_cannot_be_shadowed(self):
        data = dict(self.DATA, name="paperless-ngx")
        with self.assertRaises(core.ConvertError) as cm:
            core.save_blueprint(data)
        self.assertIn("ships with zimapp", str(cm.exception))

    def test_missing_directory_fails_loudly(self):
        """The encrypted-folder case: locked → absent. Must not fail silently."""
        core.USER_BLUEPRINT_DIR = os.path.join(self.tmp.name, "not-mounted")
        with self.assertRaises(core.ConvertError) as cm:
            core.save_blueprint(dict(self.DATA))
        self.assertIn("locked", str(cm.exception))

    def test_unwritable_directory_names_the_cause(self):
        """A bare 'Permission denied: /blueprints/x.tmp' does not tell anyone what to do.

        Hit live 2026-07-21: the container runs as uid 1000, the mounted host
        directory belonged to 999 with mode 755.
        """
        import stat
        os.chmod(self.tmp.name, 0o500)
        try:
            with self.assertRaises(core.ConvertError) as cm:
                core.save_blueprint(dict(self.DATA))
        finally:
            os.chmod(self.tmp.name, stat.S_IRWXU)
        message = str(cm.exception)
        self.assertIn("write access", message)
        self.assertIn("mode 0o500", message)
        self.assertIn("Nothing was written", message)

    def test_saving_switched_off_says_so(self):
        core.USER_BLUEPRINT_DIR = None
        with self.assertRaises(core.ConvertError) as cm:
            core.save_blueprint(dict(self.DATA))
        self.assertIn("ZIMAPP_BLUEPRINT_DIR", str(cm.exception))

    def test_without_a_source_it_refuses(self):
        with self.assertRaises(core.ConvertError):
            core.save_blueprint(dict(self.DATA, source=""))

    def test_saved_blueprints_show_up_in_the_listing(self):
        core.save_blueprint(dict(self.DATA))
        names = {b["name"]: b for b in core.list_blueprints()}
        self.assertIn("my-app", names)
        self.assertTrue(names["my-app"]["saved"])
        self.assertFalse(names["paperless-ngx"]["saved"])       # shipped one still there

    def test_dockerfile_fields_survive(self):
        """image/port/author were dropped silently — the saved recipe was unusable."""
        data = dict(self.DATA, image="ghcr.io/dir/app:1.2", port="8080", author="Someone")
        path, _ = core.save_blueprint(data)
        with open(path, encoding="utf-8") as fh:
            bp = yaml.safe_load(fh)
        self.assertEqual(bp["image"], "ghcr.io/dir/app:1.2")
        self.assertEqual(bp["port"], "8080")
        self.assertEqual(bp["author"], "Someone")

    def test_the_generator_reads_those_fields_back(self):
        """Writing them is only half — zimapp_web's merge list has to name them too."""
        import zimapp_web
        for key in ("index", "image", "port", "author"):
            self.assertIn(key, zimapp_web.BLUEPRINT_META_KEYS)

    def test_a_saved_blueprint_can_be_loaded_again(self):
        core.save_blueprint(dict(self.DATA))
        bp = core.load_blueprint("my-app")
        self.assertEqual(bp["vars"]["TZ"], "Europe/Berlin")


class BrokenYamlDiagnosis(unittest.TestCase):
    """A compose that does not parse must not be reported as 'not a compose'.

    Real case: pterodactyl/wings' own docker-compose.example.yml indents one
    volume line with a tab, which YAML forbids. zimapp said "neither a compose
    file nor a Dockerfile" — which sends you hunting for the wrong problem.
    """

    BROKEN = 'services:\n  wings:\n    volumes:\n      - "/a:/a"\n\t  - "/b:/b"\n'

    def test_it_says_the_yaml_is_broken(self):
        with self.assertRaises(core.ConvertError) as cm:
            core.detect_kind(self.BROKEN)
        self.assertIn("services:", str(cm.exception))
        self.assertIn("does not parse", str(cm.exception))

    def test_content_without_services_still_says_so(self):
        with self.assertRaises(core.ConvertError) as cm:
            core.detect_kind("hello: world\n")
        self.assertIn("neither a compose file", str(cm.exception))


class AbsoluteBindMounts(unittest.TestCase):
    """Absolute paths outside /DATA cannot work on ZimaOS — root is read-only.

    Measured on .147 (2026-07-21): `docker run -v /srv/x:/y` fails with
    "error while creating mount source path '/srv/x': mkdir /srv: read-only
    file system". The pterodactyl example compose has four of them; zimapp used
    to pass them through with a note and report "satisfies every rule".
    """

    def test_absolute_path_is_moved_under_data(self):
        result, info, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["/srv/pterodactyl/database:/var/lib/mysql"]
        """, meta={"name": "pterodactyl"})
        vol = result["services"]["app"]["volumes"][0]
        self.assertEqual(vol["source"], "/DATA/AppData/pterodactyl/database")
        self.assertTrue(any("/srv/pterodactyl/database →" in w for w in info["warnings"]))

    def test_dockers_own_state_is_not_moved(self):
        """wings mounts /var/lib/docker/containers to read logs — moving it breaks it.

        Docker Root Dir on .147 is /var/lib/docker, so that path means the daemon's
        own directory, not app data.
        """
        result, _, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["/var/lib/docker/containers/:/var/lib/docker/containers/"]
        """, meta={"name": "wings"})
        self.assertEqual(result["services"]["app"]["volumes"][0]["source"],
                         "/var/lib/docker/containers/")

    def test_tmp_stays_tmp(self):
        """/tmp is a writable tmpfs on the host — moving it makes scratch data persistent.

        wings additionally needs /tmp/pterodactyl to be the same path inside and
        outside, because it hands that path to the docker daemon.
        """
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/tmp/pterodactyl:/tmp/pterodactyl"]}
        """, meta={"name": "wings"})
        vol = result["services"]["app"]["volumes"][0]
        self.assertEqual(vol["source"], "/tmp/pterodactyl")
        self.assertEqual(vol["source"], vol["target"])

    def test_tmpfs_mounts_are_warned_about(self):
        """Measured: wings died at every boot with exit 127 because /run/wings was gone."""
        _, info, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/run/wings:/run/wings"]}
        """, meta={"name": "wings"})
        w = [x for x in info["warnings"] if "/run/wings" in x]
        self.assertTrue(w, info["warnings"])
        self.assertIn("after a reboot", w[0])
        self.assertIn("exit 127", w[0])

    def test_the_docker_socket_is_not_warned_about(self):
        """It is a socket the daemon recreates at boot — warning about it is noise."""
        _, info, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/var/run/docker.sock:/var/run/docker.sock"]}
        """, meta={"name": "app"})
        self.assertEqual([x for x in info["warnings"] if "docker.sock" in x], [])

    def test_the_reason_is_not_overstated(self):
        """/etc and /var/lib ARE writable as root — claiming otherwise is inventing.

        Measured 2026-07-21: mkdir works in /etc, /opt, /var/lib; only /srv and
        /usr/local fail. Both cases still get moved, but for different reasons.
        """
        _, info, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/etc/myapp:/config"]}
        """, meta={"name": "app"})
        moved = [w for w in info["warnings"] if "/etc/myapp →" in w][0]
        self.assertNotIn("root filesystem is read-only", moved)
        self.assertIn("Parts of the root filesystem", moved)

    def test_the_move_is_named_in_the_warning(self):
        """Silently relocating data is its own kind of lie — the target has to be visible."""
        _, info, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/srv/x/logs:/logs"]}
        """, meta={"name": "app"})
        self.assertTrue(any("/srv/x/logs → /DATA/AppData/app/logs" in w for w in info["warnings"]))

    def test_sockets_and_devices_stay(self):
        result, _, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes:
                - "/var/run/docker.sock:/var/run/docker.sock"
                - "/etc/localtime:/etc/localtime:ro"
                - "/dev/dri:/dev/dri"
        """, meta={"name": "app"})
        sources = [v["source"] for v in result["services"]["app"]["volumes"]]
        self.assertEqual(sources, ["/var/run/docker.sock", "/etc/localtime", "/dev/dri"])

    def test_real_host_locations_stay(self):
        """/media and /mnt exist on a ZimaOS box — moving them would break the point."""
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/media/disk1/films:/films"]}
        """, meta={"name": "app"})
        self.assertEqual(result["services"]["app"]["volumes"][0]["source"], "/media/disk1/films")

    def test_two_services_sharing_a_path_keep_sharing_it(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["/srv/p/shared:/a"]}
              worker: {image: nginx, volumes: ["/srv/p/shared:/b"]}
        """, meta={"name": "app"})
        self.assertEqual(result["services"]["app"]["volumes"][0]["source"],
                         result["services"]["worker"]["volumes"][0]["source"])

    def test_a_named_volume_never_collides_with_a_relocated_path(self):
        """Found by review: only absolute paths consulted the shared mapping.

        `db-data:/x` and `/opt/foo/db-data:/y` both became
        /DATA/AppData/app/db-data — an app writing into the database directory.
        """
        result, info, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["db-data:/x", "/opt/foo/db-data:/y"]
            volumes:
              db-data:
        """, meta={"name": "app"})
        sources = [v["source"] for v in result["services"]["app"]["volumes"]]
        self.assertEqual(len(set(sources)), 2, sources)
        self.assertTrue(any("db-data" in w and "already taken" in w for w in info["warnings"]),
                        info["warnings"])

    def test_a_relative_bind_never_collides_either(self):
        result, _, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["./data:/a", "/srv/x/data:/b"]
        """, meta={"name": "app"})
        sources = [v["source"] for v in result["services"]["app"]["volumes"]]
        self.assertEqual(len(set(sources)), 2, sources)

    def test_the_same_named_volume_stays_shared(self):
        result, _, _ = build("""
            services:
              app: {image: nginx, ports: ["8080:80"], volumes: ["shared:/a"]}
              worker: {image: nginx, volumes: ["shared:/b"]}
            volumes:
              shared:
        """, meta={"name": "app"})
        self.assertEqual(result["services"]["app"]["volumes"][0]["source"],
                         result["services"]["worker"]["volumes"][0]["source"])

    def test_different_paths_never_collide(self):
        """/srv/a/data and /srv/b/data must not silently become one directory."""
        result, _, _ = build("""
            services:
              app:
                image: nginx
                ports: ["8080:80"]
                volumes: ["/srv/a/data:/a", "/srv/b/data:/b"]
        """, meta={"name": "app"})
        sources = [v["source"] for v in result["services"]["app"]["volumes"]]
        self.assertEqual(len(set(sources)), 2, sources)


class EnvFileVersusInlineDefault(unittest.TestCase):
    """A compose default AND an env_file value for the same variable.

    Served from our own directory, so no network. This is the immich shape:
    `image: …:${APP_VERSION:-release}` in the compose, `APP_VERSION=v3` in the
    example.env. Compose semantics say the env file wins — and on 2026-07-21 the
    form showed 'release' while the generated tag was ':v3', and a value typed
    into the field only reached the image, not the environment block.
    """

    COMPOSE = textwrap.dedent("""
        services:
          app:
            image: app:${APP_VERSION:-release}
            ports: ["8080:80"]
            env_file: .env
    """)
    ENV = "APP_VERSION=v3\nDB_PASSWORD=pg\n"

    @classmethod
    def setUpClass(cls):
        import functools
        import http.server
        import tempfile
        import threading

        cls.tmp = tempfile.TemporaryDirectory()
        with open(cls.tmp.name + "/docker-compose.yml", "w", encoding="utf-8") as fh:
            fh.write(cls.COMPOSE)
        with open(cls.tmp.name + "/example.env", "w", encoding="utf-8") as fh:
            fh.write(cls.ENV)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=cls.tmp.name)
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.url = "http://127.0.0.1:%d/docker-compose.yml" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _build(self, variables):
        text, _ = core.build_from_source(
            self.url, {"name": "app", "title": "App", "main": "app"},
            variables, {"check_icon": False})
        doc = yaml.safe_load(text)
        return doc["services"]["app"]

    def test_env_file_wins_over_the_inline_default(self):
        """That is what compose itself does with a .env — so the tag must say v3."""
        svc = self._build({})
        self.assertEqual(svc["image"], "app:v3")
        self.assertIn("APP_VERSION=v3", svc["environment"])

    def test_a_typed_value_reaches_the_environment_too(self):
        """The regression: image said v2.1.0 while environment still said v3."""
        svc = self._build({"APP_VERSION": "v2.1.0"})
        self.assertEqual(svc["image"], "app:v2.1.0")
        self.assertIn("APP_VERSION=v2.1.0", svc["environment"])
        self.assertNotIn("APP_VERSION=v3", svc["environment"])

    def test_untouched_env_values_stay(self):
        svc = self._build({"APP_VERSION": "v2.1.0"})
        self.assertIn("DB_PASSWORD=pg", svc["environment"])

    def test_analyze_shows_the_value_that_will_be_used(self):
        import zimapp_web
        result = zimapp_web.api_analyze({"url": self.url})
        var = [v for v in result["variables"] if v["name"] == "APP_VERSION"][0]
        self.assertEqual(var["default"], "v3")               # not 'release'
        self.assertEqual(var["inline_default"], "release")   # named, not hidden
        self.assertTrue(var["from_env_file"].endswith("example.env"))


class IconSuggestion(unittest.TestCase):
    """Against our own server too — the lookup order has to hold without network.

    Background: the form used to prefill the icon field with a hardcoded
    placeholder. It was reachable, so Rule 8 reported green, and an Immich tile
    went live in the grid showing the Box.com logo (2026-07-21).
    """

    @classmethod
    def setUpClass(cls):
        import threading

        import zimapp_web
        cls.httpd = zimapp_web.ThreadingHTTPServer(("127.0.0.1", 0), zimapp_web.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.real_sources = core.ICON_SOURCES
        # First source always misses, the second one exists for app id 'icon'.
        core.ICON_SOURCES = (cls.base + "/{app_id}-missing.png", cls.base + "/{app_id}.svg")

    @classmethod
    def tearDownClass(cls):
        core.ICON_SOURCES = cls.real_sources
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_falls_through_to_the_next_source(self):
        url, tried = core.suggest_icon("icon")
        self.assertEqual(url, self.base + "/icon.svg")
        self.assertEqual(len(tried), 2)
        self.assertIn("404", tried[0][1])                  # first one was really asked
        self.assertIsNone(tried[1][1])

    def test_nothing_found_stays_empty(self):
        """No placeholder as a last resort — an empty tile is honest, a foreign logo is not."""
        url, tried = core.suggest_icon("no-such-app")
        self.assertIsNone(url)
        self.assertEqual(len(tried), 2)
        self.assertTrue(all(reason for _, reason in tried))

    def test_no_app_id(self):
        self.assertEqual(core.suggest_icon(""), (None, []))

    def test_form_does_not_prefill_an_icon(self):
        """The regression itself: no icon URL hardcoded in the form."""
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        line = [ln for ln in html.splitlines() if 'id="m-icon"' in ln]
        self.assertTrue(line, "icon field not found in the form")
        self.assertNotIn("value=", line[0].split('id="m-icon"')[1])


class Dockerfile(unittest.TestCase):

    SRC = textwrap.dedent("""
        FROM alpine:3.20
        ENV APP_HOME=/app TZ=UTC
        EXPOSE 8080 9090/udp
        VOLUME ["/data", "/config"]
        CMD ["/app/run"]
    """)

    def test_expose_env_volume(self):
        meta = core.parse_dockerfile(self.SRC)
        self.assertEqual(meta["base"], "alpine:3.20")
        self.assertEqual(meta["ports"], [8080])          # /udp does not count as the WebUI
        self.assertEqual(meta["volumes"], ["/config", "/data"])
        self.assertIn("APP_HOME=/app", meta["env"])

    def test_kind_detection(self):
        self.assertEqual(core.detect_kind(self.SRC), "dockerfile")
        self.assertEqual(core.detect_kind("services:\n  app:\n    image: x\n"), "compose")
        with self.assertRaises(core.ConvertError):
            core.detect_kind("something: without services\n")



class UpdateComparison(unittest.TestCase):
    """What ZimaOS stores is not what we sent — the diff has to know that.

    Every transformation below was read off the live system on 2026-07-21 by
    installing a known file and fetching it back. Without them, `update` would
    report changes on an app nobody touched, and the real ones would drown.
    """

    SENT = yaml.safe_load(textwrap.dedent("""
        name: demo
        services:
          app:
            image: demo/app:1.0
            environment:
            - TZ=Europe/Berlin
            - KEY=value
            deploy:
              resources:
                limits:
                  memory: 1GB
                  cpus: '1.00'
            ports:
            - mode: ingress
              target: 8080
              published: '8080'
              protocol: tcp
            networks:
            - demo-network
            depends_on:
            - db
            x-casaos:
              ports:
              - container: '8080'
                description:
                  en_us: WebUI
          db:
            image: postgres:18
        networks:
          demo-network:
            driver: bridge
        x-casaos:
          main: app
          title:
            en_us: Demo
    """))

    STORED = yaml.safe_load(textwrap.dedent("""
        name: demo
        services:
          app:
            command: null
            entrypoint: null
            image: demo/app:1.0
            environment:
              TZ: Europe/Berlin
              KEY: value
            deploy:
              placement: {}
              resources:
                limits:
                  memory: '1073741824'
                  cpus: '1.00'
            ports:
            - mode: ingress
              target: 8080
              published: '8080'
              protocol: tcp
            networks:
              demo-network: null
            depends_on:
              db:
                condition: service_started
                required: true
          db:
            command: null
            entrypoint: null
            image: postgres:18
        networks:
          default:
            name: demo_default
          demo-network:
            name: demo_demo-network
            driver: bridge
            external: false
            ipam: {}
        x-casaos:
          main: app
          store_app_id: demo
          title:
            en_us: Demo
    """))

    def test_storage_format_alone_is_not_a_change(self):
        self.assertEqual(core.compose_diff(self.STORED, self.SENT), [])

    def test_a_real_change_survives_the_normalisation(self):
        desired = yaml.safe_load(yaml.safe_dump(self.SENT))
        desired["services"]["app"]["image"] = "demo/app:2.0"
        changes = core.compose_diff(self.STORED, desired)
        self.assertEqual([c["path"] for c in changes], ["services.app.image"])
        self.assertEqual(changes[0]["installed"], "demo/app:1.0")
        self.assertEqual(changes[0]["desired"], "demo/app:2.0")

    def test_a_stricter_depends_on_condition_is_a_change(self):
        desired = yaml.safe_load(yaml.safe_dump(self.SENT))
        desired["services"]["app"]["depends_on"] = {"db": {"condition": "service_healthy"}}
        changes = core.compose_diff(self.STORED, desired)
        self.assertEqual([c["path"] for c in changes], ["services.app.depends_on.db"])

    def test_added_and_removed_are_named_as_such(self):
        desired = yaml.safe_load(yaml.safe_dump(self.SENT))
        desired["services"]["app"]["environment"].append("NEW=1")
        desired["services"]["app"]["environment"].remove("KEY=value")
        kinds = {c["path"]: c["kind"] for c in core.compose_diff(self.STORED, desired)}
        self.assertEqual(kinds, {"services.app.environment.NEW": "add",
                                 "services.app.environment.KEY": "remove"})

    def test_image_reference_is_compared_by_meaning(self):
        self.assertEqual(core._image_key("redis"), "redis:latest")
        self.assertEqual(core._image_key("docker.io/library/redis:8"), "redis:8")
        self.assertEqual(core._image_key("ghcr.io/a/b"), "ghcr.io/a/b:latest")
        self.assertEqual(core._image_key("a/b@sha256:abc"), "a/b@sha256:abc")


class UpdateKeepsWhatRuns(unittest.TestCase):
    """A regenerated password is not a new password, it is a broken app."""

    INSTALLED = {"services": {"db": {"environment": {"POSTGRES_PASSWORD": "old-secret",
                                                     "POSTGRES_USER": "paperless"}}}}

    def _doc(self):
        return {"services": {"db": {"environment": ["POSTGRES_PASSWORD=fresh-secret",
                                                    "POSTGRES_USER=admin"]}}}

    def test_generated_values_do_not_overwrite_running_ones(self):
        doc = self._doc()
        kept = core.keep_installed_values(doc, self.INSTALLED, {"POSTGRES_PASSWORD": "fresh-secret"})
        self.assertEqual(kept, ["POSTGRES_PASSWORD"])
        self.assertIn("POSTGRES_PASSWORD=old-secret", doc["services"]["db"]["environment"])

    def test_an_explicit_value_stays_a_visible_change(self):
        doc = self._doc()
        core.keep_installed_values(doc, self.INSTALLED, {"POSTGRES_PASSWORD": "fresh-secret"})
        # POSTGRES_USER was not generated — it is a deliberate change and stays.
        self.assertIn("POSTGRES_USER=admin", doc["services"]["db"]["environment"])

    def test_force_keeps_a_value_that_was_not_generated(self):
        doc = self._doc()
        kept = core.keep_installed_values(doc, self.INSTALLED, {}, force=["POSTGRES_USER"])
        self.assertEqual(kept, ["POSTGRES_USER"])
        self.assertIn("POSTGRES_USER=paperless", doc["services"]["db"]["environment"])

    def test_a_renamed_service_keeps_nothing_and_says_so_by_omission(self):
        doc = {"services": {"database": {"environment": ["POSTGRES_PASSWORD=fresh-secret"]}}}
        kept = core.keep_installed_values(doc, self.INSTALLED, {"POSTGRES_PASSWORD": "fresh-secret"})
        self.assertEqual(kept, [])
        self.assertIn("POSTGRES_PASSWORD=fresh-secret", doc["services"]["database"]["environment"])

    def test_metadata_comes_back_out_of_the_installation(self):
        installed = {"name": "demo", "services": {"app": {"deploy": {"resources": {"limits": {
            "memory": "1073741824", "cpus": "1.00"}}}}},
            "x-casaos": {"main": "app", "author": "someone", "category": "Documents",
                         "icon": "http://i/x.png", "title": {"en_us": "Demo", "custom": "Demo!"},
                         "tagline": {"en_us": "short"}}}
        meta = core.meta_from_installed(installed)
        self.assertEqual(meta["title"], "Demo!")
        self.assertEqual(meta["category"], "Documents")
        self.assertEqual(meta["memory"], "1073741824")
        self.assertEqual(meta["main"], "app")

    def test_values_for_source_variables_are_carried_over(self):
        installed = {"services": {"app": {"environment": {"UPLOAD_LOCATION": "/DATA/x",
                                                          "OTHER": "y"}}}}
        self.assertEqual(core.carry_over_values(installed, {"UPLOAD_LOCATION", "MISSING"}),
                         {"UPLOAD_LOCATION": "/DATA/x"})


class UpdateCompletionSignal(unittest.TestCase):
    """The stored compose is not a witness for what an app runs.

    Measured 2026-07-21 on the live system: a PUT with an image that cannot be
    pulled answers HTTP 200, appears in the stored compose (in one run for ~7s,
    in the next for over 21s), and the app keeps reporting 'running' the whole
    time — because the OLD container never stopped. Only GET .../containers
    tells the truth. These tests keep that lesson from being optimised away.
    """

    DESIRED = {"services": {"app": {"image": "demo/app:2.0"}}}

    def setUp(self):
        self._login, self._sleep = core.login, core.time.sleep
        self._installed, self._containers = core.installed_compose, core.running_containers
        core.login = lambda h, u, p: "token"
        core.time.sleep = lambda s: None

    def tearDown(self):
        core.login, core.time.sleep = self._login, self._sleep
        core.installed_compose, core.running_containers = self._installed, self._containers

    def test_a_stored_compose_alone_does_not_count_as_applied(self):
        core.installed_compose = lambda *a, **kw: (self.DESIRED, "running(1)", "token")
        core.running_containers = lambda *a, **kw: (
            {"app": {"image": "demo/app:1.0", "state": "running", "status": "Up 2 hours",
                     "exit_code": 0, "health": "", "id": "abc"}}, "token")
        result = core.wait_for_update("h", "demo", self.DESIRED, token="token",
                                      timeout=6, interval=3)
        self.assertFalse(result["applied"])
        self.assertEqual(result["remaining"], [])
        self.assertIn("runs demo/app:1.0", result["running_problems"][0])

    def test_applied_needs_the_container_to_run_it(self):
        core.installed_compose = lambda *a, **kw: (self.DESIRED, "running(1)", "token")
        core.running_containers = lambda *a, **kw: (
            {"app": {"image": "demo/app:2.0", "state": "running", "status": "Up 3 seconds",
                     "exit_code": 0, "health": "", "id": "def"}}, "token")
        result = core.wait_for_update("h", "demo", self.DESIRED, token="token",
                                      timeout=6, interval=3)
        self.assertTrue(result["applied"])

    def test_a_container_that_exited_is_not_applied(self):
        core.installed_compose = lambda *a, **kw: (self.DESIRED, "running(1)", "token")
        core.running_containers = lambda *a, **kw: (
            {"app": {"image": "demo/app:2.0", "state": "exited", "status": "Exited (127)",
                     "exit_code": 127, "health": "", "id": "def"}}, "token")
        result = core.wait_for_update("h", "demo", self.DESIRED, token="token",
                                      timeout=6, interval=3)
        self.assertFalse(result["applied"])
        self.assertIn("exited", result["running_problems"][0])

    def test_a_missing_container_is_named(self):
        core.installed_compose = lambda *a, **kw: (self.DESIRED, "running(1)", "token")
        core.running_containers = lambda *a, **kw: ({}, "token")
        result = core.wait_for_update("h", "demo", self.DESIRED, token="token",
                                      timeout=3, interval=3)
        self.assertFalse(result["applied"])
        self.assertIn("no container at all", result["running_problems"][0])




class UpdateReviewFindings(unittest.TestCase):
    """Four defects the review of `update` turned up, each measured before the fix.

    They are one family: a check that silently covers less than it claims to.
    """

    def test_deploy_settings_beyond_memory_and_cpus_are_compared(self):
        # Before the fix the comparison kept only memory/cpus, so this was []
        # while `update` claimed to list every difference.
        old = {"services": {"app": {"deploy": {"replicas": 1,
                                               "restart_policy": {"condition": "any"}}}}}
        new = {"services": {"app": {"deploy": {"replicas": 3,
                                               "restart_policy": {"condition": "none"}}}}}
        paths = [c["path"] for c in core.compose_diff(old, new)]
        self.assertIn("services.app.deploy.replicas", paths)
        self.assertIn("services.app.deploy.restart_policy.condition", paths)

    def test_the_storage_format_of_deploy_is_still_no_difference(self):
        stored = {"services": {"app": {"deploy": {"placement": {}, "resources": {
            "limits": {"memory": "1073741824", "cpus": "1.00"}}}}}}
        sent = {"services": {"app": {"deploy": {"resources": {
            "limits": {"memory": "1GB", "cpus": "1.00"}}}}}}
        self.assertEqual(core.compose_diff(stored, sent), [])

    def test_normalising_does_not_change_the_document_it_was_given(self):
        doc = {"services": {"app": {"deploy": {"placement": {}, "resources": {
            "limits": {"memory": "1GB"}}}}}}
        core.normalize_for_compare(doc)
        self.assertEqual(doc["services"]["app"]["deploy"]["placement"], {})
        self.assertEqual(doc["services"]["app"]["deploy"]["resources"]["limits"]["memory"], "1GB")

    def test_a_container_the_new_definition_no_longer_has_is_reported(self):
        desired = {"services": {"app": {"image": "x:1"}}}
        containers = {"app": {"image": "x:1", "state": "running", "status": "Up",
                              "exit_code": 0, "health": "", "id": "1"},
                      "db": {"image": "postgres:18", "state": "running", "status": "Up 3 hours",
                             "exit_code": 0, "health": "", "id": "2"}}
        problems = core.running_mismatch(desired, containers)
        self.assertEqual(len(problems), 1)
        self.assertIn("db", problems[0])
        self.assertIn("no longer has that service", problems[0])

    def test_a_secret_that_moves_to_a_renamed_service_is_called_out(self):
        doc = {"services": {"database": {"environment": ["POSTGRES_PASSWORD=fresh"]}}}
        installed = {"services": {"db": {"environment": {"POSTGRES_PASSWORD": "old"}}}}
        generated = {"POSTGRES_PASSWORD": "fresh"}
        kept = core.keep_installed_values(doc, installed, generated)
        self.assertEqual(kept, [])          # the service name no longer matches
        notes = core.regenerated_elsewhere(doc, installed, generated, kept)
        self.assertEqual(len(notes), 1)
        self.assertIn("REGENERATED", notes[0])
        self.assertIn("'db'", notes[0])
        self.assertIn("'database'", notes[0])

    def test_no_note_when_the_value_was_kept(self):
        doc = {"services": {"db": {"environment": ["POSTGRES_PASSWORD=fresh"]}}}
        installed = {"services": {"db": {"environment": {"POSTGRES_PASSWORD": "old"}}}}
        generated = {"POSTGRES_PASSWORD": "fresh"}
        kept = core.keep_installed_values(doc, installed, generated)
        self.assertEqual(core.regenerated_elsewhere(doc, installed, generated, kept), [])

    def test_flags_that_cannot_act_are_refused_before_the_network(self):
        """--keep with --file was a silent no-op. It has to fail, and fail early."""
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as tmp:
            compose = os.path.join(tmp, "app.yml")
            with open(compose, "w", encoding="utf-8") as fh:
                fh.write("name: demo\nservices:\n  app:\n    image: x:1\n")
            proc = subprocess.run(
                [sys.executable, os.path.join(here, "zimapp.py"), "update", "demo",
                 "--file", compose, "--keep", "SECRET",
                 # a host that must never be contacted: the guard comes first
                 "--host", "192.0.2.1", "--user", "u", "--pass", "p"],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--keep would do nothing", proc.stderr)




class FrameworkTraps(unittest.TestCase):
    """Three ways an app comes up healthy and is still unusable.

    ZimaOS sees none of them: container running, port answering, tile there.
    The checks read their evidence out of the compose — none of them guesses a
    framework from the image name, because "looks like Django" is not a
    measurement.
    """

    def _svc(self, env, ports=None):
        return {"app": {"image": "x:1", "ports": ports or [
            {"mode": "ingress", "target": 8000, "published": "8123", "protocol": "tcp"}],
            "environment": env}}

    def test_own_url_naming_a_port_it_is_not_reachable_on(self):
        _, warnings = core.framework_checks(self._svc(["APP_URL=http://host.lan:8000"]))
        self.assertEqual(len(warnings), 1)
        self.assertIn("names port 8000", warnings[0])
        self.assertIn("8123", warnings[0])
        self.assertIn("reverse proxy", warnings[0])      # the case where it IS correct

    def test_a_matching_url_says_nothing(self):
        problems, warnings = core.framework_checks(self._svc(["APP_URL=http://host.lan:8123"]))
        self.assertEqual((problems, warnings), ([], []))

    def test_https_without_a_port_means_443(self):
        _, warnings = core.framework_checks(self._svc(["SITE_URL=https://host.lan"]))
        self.assertIn("names port 443", warnings[0])
        _, none = core.framework_checks(self._svc(
            ["SITE_URL=https://host.lan"],
            ports=[{"mode": "ingress", "target": 8000, "published": "443", "protocol": "tcp"}]))
        self.assertEqual(none, [])

    def test_a_url_that_is_not_a_url_is_not_guessed_at(self):
        problems, warnings = core.framework_checks(self._svc(["APP_URL=host.lan"]))
        self.assertEqual((problems, warnings), ([], []))

    def test_a_well_known_placeholder_secret_is_an_error(self):
        problems, _ = core.framework_checks(self._svc(["SECRET_KEY=change_me"]))
        self.assertEqual(len(problems), 1)
        self.assertIn("well-known placeholder", problems[0])

    def test_an_empty_secret_is_a_warning_and_not_an_error(self):
        """Measured on the live pterodactyl install: MAIL_PASSWORD is empty on
        purpose and everything works. Calling that an error would teach people
        to ignore the whole class of message."""
        problems, warnings = core.framework_checks(self._svc(["MAIL_PASSWORD="]))
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("optional feature", warnings[0])

    def test_a_generated_secret_says_nothing(self):
        problems, warnings = core.framework_checks(
            self._svc(["SECRET_KEY=U4UD8EnwGUoELp-5kxyA1k7lLur"]))
        self.assertEqual((problems, warnings), ([], []))

    def test_an_admin_user_without_a_password(self):
        _, warnings = core.framework_checks(self._svc(["PAPERLESS_ADMIN_USER=admin"]))
        self.assertEqual(len(warnings), 1)
        self.assertIn("nobody can log in", warnings[0])

    def test_a_complete_admin_account_says_nothing(self):
        problems, warnings = core.framework_checks(
            self._svc(["PAPERLESS_ADMIN_USER=admin", "PAPERLESS_ADMIN_PASSWORD=s3cr3t"]))
        self.assertEqual((problems, warnings), ([], []))

    def test_the_checks_reach_the_validator(self):
        text = textwrap.dedent("""
            name: demo
            services:
              app:
                image: x:1
                ports:
                - mode: ingress
                  target: 8000
                  published: '8123'
                  protocol: tcp
                environment:
                - SECRET_KEY=changeme
            x-casaos:
              main: app
              port_map: '8123'
              icon: http://i/x.png
              title:
                en_us: Demo
              description:
                en_us: Demo
        """)
        problems, _ = core.validate(text)
        self.assertTrue(any("well-known placeholder" in p for p in problems), problems)



class BlueprintDrift(unittest.TestCase):
    """Has upstream moved under a blueprint — without needing a ZimaOS host.

    The fingerprint is deliberately a hash and not a stored compose: a copy is
    what makes a catalogue rot. A hash answers only "is this still the file
    someone looked at", which is exactly what a "tested" badge cannot answer
    for itself.
    """

    VALID_COMPOSE = textwrap.dedent("""
        name: demo
        services:
          app:
            image: x:1
            ports:
            - mode: ingress
              target: 8000
              published: '8123'
              protocol: tcp
        x-casaos:
          main: app
          port_map: '8123'
          icon: http://i/x.png
          title:
            en_us: Demo
          description:
            en_us: Demo
    """)

    def test_line_endings_do_not_change_the_fingerprint(self):
        self.assertEqual(core.source_fingerprint("a\nb\n"), core.source_fingerprint("a\r\nb\r\n"))

    def test_content_does(self):
        self.assertNotEqual(core.source_fingerprint("a\n"), core.source_fingerprint("b\n"))

    def _blueprint(self, tmp, recorded, source):
        path = os.path.join(tmp, "demo.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(f"""
                name: demo
                title: Demo
                source: {source}
                verified:
                  date: '2026-07-01'
                  source_seen: '2026-07-01'
                  source_sha256: {recorded}
            """))
        return path

    def _run(self, tmp, recorded, upstream="services:\n  app:\n    image: x:1\n    ports:\n      - '8123:8000'\n"):
        """Drift check with the network and the conversion stubbed out."""
        original_dir, original_fetch, original_build = (
            core.BLUEPRINT_DIR, core.fetch_source, core.build_from_source)
        core.BLUEPRINT_DIR = tmp
        core.fetch_source = lambda url, timeout=30: (upstream, url)
        core.build_from_source = lambda *a, **kw: (self.VALID_COMPOSE,
                                                   {"web_port": "8123", "app_id": "demo"})
        try:
            return core.check_blueprint_drift("demo")
        finally:
            core.BLUEPRINT_DIR, core.fetch_source, core.build_from_source = (
                original_dir, original_fetch, original_build)

    def test_an_unchanged_source_is_ok(self):
        upstream = "services: {}\n"
        with tempfile.TemporaryDirectory() as tmp:
            self._blueprint(tmp, core.source_fingerprint(upstream), "http://u/x.yml")
            result = self._run(tmp, None, upstream)
        self.assertEqual(result["status"], "ok")

    def test_a_changed_source_is_reported_with_the_date_it_was_last_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._blueprint(tmp, "a" * 64, "http://u/x.yml")
            result = self._run(tmp, None)
        self.assertEqual(result["status"], "moved")
        self.assertIn("2026-07-01", result["notes"][0])

    def test_a_hash_yaml_turned_into_a_number_is_named_as_such(self):
        """0000... is an int to YAML, and a falsy one. Without the str() this
        read as 'no fingerprint recorded' — the check would have gone quiet."""
        with tempfile.TemporaryDirectory() as tmp:
            self._blueprint(tmp, "0" * 64, "http://u/x.yml")
            result = self._run(tmp, None)
        self.assertEqual(result["status"], "moved")
        self.assertTrue(any("not a sha256" in n for n in result["notes"]), result["notes"])

    def test_a_blueprint_without_a_fingerprint_is_not_called_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._blueprint(tmp, "x" * 64, "http://u/x.yml")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text.replace(f"  source_sha256: {'x' * 64}\n", ""))
            result = self._run(tmp, None)
        self.assertEqual(result["status"], "unrecorded")
        self.assertIn("nothing to compare against", result["notes"][0])



if __name__ == "__main__":
    unittest.main()
