# -*- coding: utf-8 -*-
"""Regression tests for model resolution and the self-upgrade guards.

Run with:  python test_hexsec.py

Network is stubbed, so these are deterministic and offline-safe.
"""
import io
import json
import os
import sys
import tempfile
import shutil
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import SeeOpenRouterFreeModels as disc
import HexSecGPT as app_mod
import httpx
FAKE_CATALOGUE = {
    "data": [
        {"id": "vendor/good-model:free", "pricing": {"prompt": "0", "completion": "0"},
         "context_length": 128000,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "vendor/zero-priced", "pricing": {"prompt": 0, "completion": 0},
         "context_length": 4096,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "vendor/paid-model", "pricing": {"prompt": "0.000003", "completion": "0.00001"},
         "context_length": 8000,
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        # Id fragments that EXCLUDED_FRAGMENTS is contracted to reject.
        {"id": "nvidia/content-safety-9b:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "vendor/auto-router:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        # Priced at -1 (OpenRouter's "unknown pricing" sentinel) with NO :free
        # suffix, so the pricing path - not the suffix - decides. Not free.
        {"id": "vendor/unpriced-model", "pricing": {"prompt": "-1", "completion": "-1"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        # Zero-priced but produces audio, not text.
        {"id": "vendor/music-model:free", "pricing": {"prompt": "0", "completion": "0"},
         "architecture": {"input_modalities": ["text"], "output_modalities": ["text", "audio"]}},
    ]
}


def write_file(path, content, mode="w"):
    """Write content to path, closing the handle (keeps the suite warning-free)."""
    with open(path, mode) as handle:
        handle.write(content)
    return path


def write_json(path, payload):
    return write_file(path, json.dumps(payload))

def stub_catalogue(monkey_target, payload=None, raise_exc=None):
    """Replace fetch_models with a fixed catalogue."""
    payload = payload if payload is not None else FAKE_CATALOGUE["data"]
    target = monkey_target

    def _fetch(force_refresh=False, cache_path=None, timeout=15):
        if raise_exc:
            raise raise_exc
        return payload

    target.fetch_models = _fetch
    return target


class TestPricingDetection(unittest.TestCase):
    """The original bug: pricing is a string, so `== 0` never matched."""

    def test_string_zero_counts_as_free(self):
        self.assertTrue(disc.is_free_model(FAKE_CATALOGUE["data"][0]))

    def test_int_zero_counts_as_free(self):
        self.assertTrue(disc.is_free_model(FAKE_CATALOGUE["data"][1]))

    def test_paid_model_is_not_free(self):
        self.assertFalse(disc.is_free_model(FAKE_CATALOGUE["data"][2]))

    def test_minus_one_is_not_free(self):
        # -1 is OpenRouter's "pricing unknown" sentinel, not free.
        self.assertFalse(disc.is_free_model(FAKE_CATALOGUE["data"][5]))

    def test_suffix_wins_even_with_paid_pricing(self):
        self.assertTrue(disc.is_free_model({"id": "x/y:free",
                                            "pricing": {"prompt": "0.1", "completion": "0.2"}}))

    def test_missing_pricing_is_not_free(self):
        self.assertFalse(disc.is_free_model({"id": "x/y"}))


class TestChatSuitability(unittest.TestCase):
    def test_audio_only_output_rejected(self):
        # index 6 produces audio as well as text; still usable, but the
        # pure-audio case is what matters.
        self.assertTrue(disc.is_chat_model(FAKE_CATALOGUE["data"][6]))
        self.assertFalse(disc.is_chat_model(
            {"architecture": {"output_modalities": ["audio"]}}))

    def test_text_model_accepted(self):
        self.assertTrue(disc.is_chat_model(FAKE_CATALOGUE["data"][0]))

    def test_safety_classifier_excluded(self):
        self.assertFalse(disc.is_suitable_for_chat("nvidia/content-safety-9b:free"))

    def test_router_model_excluded(self):
        self.assertFalse(disc.is_suitable_for_chat("vendor/auto-router:free"))

    def test_normal_model_included(self):
        self.assertTrue(disc.is_suitable_for_chat("vendor/good-model:free"))


class TestFreeModelListing(unittest.TestCase):
    def setUp(self):
        self._real_fetch = disc.fetch_models
        self._real_override = disc.load_model_override
        disc.load_model_override = lambda: None
        self.cache = os.path.join(tempfile.mkdtemp(), "cache.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.cache), True)

    def tearDown(self):
        disc.fetch_models = self._real_fetch
        disc.load_model_override = self._real_override

    def test_listing_excludes_paid_and_unsuitable(self):
        stub_catalogue(disc)
        ids = disc.list_free_models(cache_path=self.cache)
        self.assertIn("vendor/good-model:free", ids)
        self.assertIn("vendor/zero-priced", ids)
        self.assertNotIn("vendor/paid-model", ids)
        self.assertNotIn("nvidia/content-safety-9b:free", ids)
        self.assertNotIn("vendor/auto-router:free", ids)
        self.assertNotIn("vendor/unpriced-model", ids)

    def test_stale_pin_falls_back_instead_of_failing(self):
        stub_catalogue(disc)
        chosen = disc.resolve_free_model("vendor/retired-model:free",
                                        cache_path=self.cache)
        self.assertIn(chosen, disc.list_free_models(cache_path=self.cache))
        self.assertNotEqual(chosen, "vendor/retired-model:free")

    def test_live_pin_is_honoured(self):
        stub_catalogue(disc)
        self.assertEqual(
            disc.resolve_free_model("vendor/good-model:free", cache_path=self.cache),
            "vendor/good-model:free",
        )

    def test_no_free_models_returns_none(self):
        stub_catalogue(disc, payload=[FAKE_CATALOGUE["data"][2]])
        self.assertIsNone(disc.resolve_free_model(cache_path=self.cache))

    def test_stale_cache_is_used_when_fresh(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        cache = os.path.join(tmpdir, "c.json")
        with open(cache, "w") as f:
            json.dump({"fetched_at": __import__("time").time(), "models": FAKE_CATALOGUE["data"]}, f)
        # No stub: a fresh cache must satisfy the call without any network I/O.
        self.assertEqual(len(disc.fetch_models(cache_path=cache)), len(FAKE_CATALOGUE["data"]))

    def test_stale_cache_rescues_failed_network(self):
        """A network failure must degrade to the last known catalogue."""
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        cache = os.path.join(tmpdir, "c.json")
        with open(cache, "w") as f:
            json.dump({"fetched_at": 0, "models": FAKE_CATALOGUE["data"]}, f)

        import requests
        real_get = requests.get

        def _boom(*a, **k):
            raise requests.ConnectionError("offline")

        requests.get = _boom
        try:
            # force_refresh skips the fresh-cache check, so the network is hit
            # and fails - the cached catalogue must still be returned.
            models = disc.fetch_models(force_refresh=True, cache_path=cache)
        finally:
            requests.get = real_get

        self.assertEqual(len(models), len(FAKE_CATALOGUE["data"]))

    def test_network_failure_without_cache_raises(self):
        stub_catalogue(disc, raise_exc=disc.ModelDiscoveryError("offline"))
        with self.assertRaises(disc.ModelDiscoveryError):
            disc.fetch_models(force_refresh=True, cache_path=self.cache)


class TestModelResolutionInApp(unittest.TestCase):
    """The app must never start pinned to the literal string 'auto'."""

    def setUp(self):
        self._real = disc.resolve_free_model
        self._real_list = disc.list_free_models
        self._real_exists = disc.model_exists
        self._ui = app_mod.UI()
        self.brain = app_mod.HexSecBrain("sk-test", self._ui)

    def tearDown(self):
        disc.resolve_free_model = self._real
        disc.list_free_models = self._real_list
        disc.model_exists = self._real_exists

    def test_auto_placeholder_is_resolved(self):
        disc.resolve_free_model = lambda *a, **k: "vendor/live:free"
        self.assertEqual(self.brain.resolve_model(), "vendor/live:free")
        self.assertNotEqual(self.brain.model, app_mod.Config.AUTO_MODEL)

    def test_resolution_is_cached_not_repeated(self):
        calls = []

        def _counting(*a, **k):
            calls.append(1)
            return "vendor/live:free"

        disc.resolve_free_model = _counting
        self.brain.resolve_model()
        self.brain.resolve_model()
        self.assertEqual(len(calls), 1)

    def test_discovery_failure_keeps_placeholder_visible(self):
        def _boom(*a, **k):
            raise RuntimeError("no network")

        disc.resolve_free_model = _boom
        self.brain.resolve_model()
        self.assertEqual(self.brain.model, app_mod.Config.AUTO_MODEL)

    def test_explicit_model_is_not_overwritten(self):
        pinned = app_mod.HexSecBrain("sk-test", self._ui, model_override="vendor/pinned:free")
        disc.resolve_free_model = lambda *a, **k: "vendor/other:free"
        self.assertEqual(pinned.resolve_model(), "vendor/pinned:free")


class TestModelFailureDetection(unittest.TestCase):
    def test_model_errors_are_retriable(self):
        for text in ("model_not_found", "404 not found", "rate limit exceeded",
                     "no endpoints available", "503"):
            self.assertTrue(app_mod.HexSecBrain._is_model_failure(Exception(text)), text)

    def test_auth_and_generic_errors_are_not(self):
        for text in ("401 unauthorized", "invalid api key", "connection reset"):
            self.assertFalse(app_mod.HexSecBrain._is_model_failure(Exception(text)), text)


class TestChatFallback(unittest.TestCase):
    """A retired model must switch models instead of ending the chat."""

    def setUp(self):
        self._real_list = disc.list_free_models
        self._real_score = disc._score
        disc.list_free_models = lambda **k: ["vendor/backup:free"]
        disc._score = lambda mid: (0, 0, mid)

    def tearDown(self):
        disc.list_free_models = self._real_list
        disc._score = self._real_score

    def _brain(self):
        brain = app_mod.HexSecBrain("sk-test", app_mod.UI(), model_override="vendor/dead:free")
        brain._resolved = True
        return brain

    def test_dead_model_triggers_switch(self):
        brain = self._brain()
        calls = []

        def _create(**kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise Exception("404 model_not_found")
            return iter([
                type("C", (), {"choices": [type("D", (), {"delta": type("E", (), {"content": "hi"})()})()]})()
            ])

        brain.client = type("Client", (), {
            "chat": type("Chat", (), {"completions": type("Comp", (), {"create": staticmethod(_create)})()})()
        })()

        out = "".join(brain.chat("hi"))
        self.assertEqual(calls, ["vendor/dead:free", "vendor/backup:free"])
        self.assertIn("hi", out)

    def test_auth_error_does_not_switch_models(self):
        brain = self._brain()
        calls = []

        def _create(**kwargs):
            calls.append(kwargs["model"])
            # A real 401 response, so the SDK builds a genuine
            # AuthenticationError instead of raising on a None response.
            response = httpx.Response(
                401,
                request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
                json={"error": {"message": "Invalid API key"}},
            )
            raise app_mod.openai.AuthenticationError(
                "Invalid API key", response=response, body=None
            )

        brain.client = type("Client", (), {
            "chat": type("Chat", (), {"completions": type("Comp", (), {"create": staticmethod(_create)})()})()
        })()

        out = "".join(brain.chat("hi"))
        self.assertEqual(calls, ["vendor/dead:free"])
        self.assertIn("401", out)


class TestUpgradeGuards(unittest.TestCase):
    """The self-upgrade protections from the audit must hold."""

    def setUp(self):
        from upgrademanger import SelfUpgradingManager
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "version.json"), "w") as f:
            f.write('{"version": "1.0.0"}')
        cfg = {"version_file": "version.json", "backup_enabled": True,
               "parallel_processing": False, "rollback_enabled": True, "max_backups": 3}
        self.cfgp = os.path.join(self.tmp, "upgrade_config.json")
        with open(self.cfgp, "w") as f:
            json.dump(cfg, f)
        self.m = SelfUpgradingManager(self.tmp, self.cfgp)

    def test_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.m._resolve_within_root("../../escape.py")
        with self.assertRaises(ValueError):
            self.m._resolve_within_root(os.path.abspath("/etc/passwd"))

    def test_in_project_path_allowed(self):
        resolved = self.m._resolve_within_root("src/ok.py")
        self.assertTrue(resolved.startswith(os.path.abspath(self.tmp)))

    def test_malicious_manifest_rejected_before_writing(self):
        pkg = tempfile.mkdtemp(dir=self.tmp)
        write_file(os.path.join(pkg, "payload.py"), "# evil\n")
        write_json(os.path.join(pkg, "manifest.json"),
                   {"files_to_update": [{"source": "payload.py",
                                         "destination": "../../victim.py"}]})
        self.assertFalse(self.m.apply_upgrade_patch(pkg, "9.9.9"))
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self.tmp), "victim.py")))

    def test_backup_excludes_secrets(self):
        write_file(os.path.join(self.tmp, ".HexSec"), "KEY=secret\n")
        write_file(os.path.join(self.tmp, ".env"), "K=v\n")
        write_file(os.path.join(self.tmp, "code.py"), "x=1\n")
        self.m.scan_project_structure()
        backup = self.m.create_backup()
        copied = {f for _, _, fs in os.walk(backup) for f in fs}
        self.assertNotIn(".HexSec", copied)
        self.assertNotIn(".env", copied)
        self.assertIn("code.py", copied)

    def test_hash_mismatch_blocks_write(self):
        src = write_file(os.path.join(self.tmp, "s.py"), "data")
        dst = os.path.join(self.tmp, "d.py")
        self.assertIsNone(self.m.process_task(("add_file", src, dst, "0" * 64)))
        self.assertFalse(os.path.exists(dst))

    def test_scan_handles_root_files(self):
        write_file(os.path.join(self.tmp, "README.md"), "# x\n")
        structure = self.m.scan_project_structure()
        self.assertIn("README.md", structure["files"])

    def test_rollback_restores_root_files(self):
        write_file(os.path.join(self.tmp, "root.py"), "original")
        self.m.scan_project_structure()
        backup = self.m.create_backup()
        write_file(os.path.join(self.tmp, "root.py"), "CORRUPTED")
        self.assertTrue(self.m.rollback(backup))
        with open(os.path.join(self.tmp, "root.py")) as f:
            self.assertEqual(f.read(), "original")

    def test_rollback_tolerates_legacy_metadata(self):
        self.m.scan_project_structure()
        backup = self.m.create_backup()
        write_file(os.path.join(backup, "backup_metadata.json"), '{"modules": 3}')
        self.assertTrue(self.m.rollback(backup))

    def test_unsigned_download_refused(self):
        self.m.config["signature_key"] = ""
        self.assertIsNone(self.m.download_upgrade_package("2.0.0"))

    def test_http_update_server_refused(self):
        self.m.config["update_server"] = "http://insecure.example.com"
        self.assertIsNone(self.m.get_available_upgrades())

    def test_signature_verification(self):
        import hmac, hashlib
        self.m.config["signature_key"] = "k"
        pkg = write_file(os.path.join(self.tmp, "p.zip"), b"body", "wb")
        good = write_file(os.path.join(self.tmp, "good.sig"),
                          hmac.new(b"k", b"body", hashlib.sha256).hexdigest())
        bad = write_file(os.path.join(self.tmp, "bad.sig"), "00")
        self.assertTrue(self.m._verify_package_signature(pkg, good))
        self.assertFalse(self.m._verify_package_signature(pkg, bad))
        self.assertFalse(self.m._verify_package_signature(pkg, None))

    def test_zip_slip_blocked(self):
        import zipfile
        zp = os.path.join(self.tmp, "e.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("../../../../hexsec_test_canary.txt", "x")
        self.assertIsNone(self.m.extract_upgrade_package(zp))


class TestCLI(unittest.TestCase):
    def test_list_models_flag(self):
        stub_catalogue(disc)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = disc.main([])
        self.assertEqual(rc, 0)
        self.assertIn("vendor/good-model:free", buf.getvalue())

    def test_app_help_exits_cleanly(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                app_mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--list-models", buf.getvalue())

class TestStartupReachesMenuWithoutKey(unittest.TestCase):
    """Option [2] sets the API key, so the menu must render without one.

    The original startup gated the menu behind a verified key, which made
    the very option that configures a key unreachable without one.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Point the key store at an empty temp file so the real .HexSec is
        # never read or written.
        real_env_file = app_mod.Config.ENV_FILE
        app_mod.Config.ENV_FILE = os.path.join(self.tmp, ".HexSec")
        self.addCleanup(setattr, app_mod.Config, "ENV_FILE", real_env_file)
        # load_dotenv() writes into os.environ; a key from the developer's
        # real environment would otherwise authenticate these tests.
        for var in (app_mod.Config.API_KEY_NAME, "HEXSEC_MODEL"):
            saved = os.environ.pop(var, None)
            self.addCleanup(self._restore_env, var, saved)

    @staticmethod
    def _restore_env(var, value):
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value

    def _run_menu(self, choices):
        """Drive App.start() with scripted input; return what each branch did."""
        calls = {"menu": 0, "chat": 0, "about": 0, "configure": 0, "msgs": []}

        saved = {name: getattr(app_mod.UI, name) for name in
                 ("get_input", "main_menu", "show_msg", "banner")}
        saved.update({name: getattr(app_mod.App, name) for name in
                      ("about", "run_chat", "configure_key")})
        self.addCleanup(self._restore_ui, saved)

        app_mod.UI.banner = lambda self: None
        app_mod.UI.main_menu = lambda self: calls.__setitem__("menu", calls["menu"] + 1)
        app_mod.UI.show_msg = (
            lambda self, title, content, color="white": calls["msgs"].append((title, content)))
        app_mod.App.about = lambda self: calls.__setitem__("about", calls["about"] + 1)
        app_mod.App.run_chat = lambda self: calls.__setitem__("chat", calls["chat"] + 1)
        app_mod.App.configure_key = (
            lambda self: calls.__setitem__("configure", calls["configure"] + 1) or False)

        remaining = list(choices)
        app_mod.UI.get_input = lambda self, label="COMMAND": remaining.pop(0) if remaining else "4"

        with self.assertRaises(SystemExit):
            app_mod.App().start()
        return calls

    @staticmethod
    def _restore_ui(saved):
        for name, value in saved.items():
            owner = app_mod.App if hasattr(app_mod.App, name) else app_mod.UI
            setattr(owner, name, value)

    def test_menu_renders_with_no_key(self):
        self.assertGreater(self._run_menu(["3", "", "4"])["menu"], 0)

    def test_start_chat_is_refused_without_key(self):
        calls = self._run_menu(["1", "3", "", "4"])
        self.assertEqual(calls["chat"], 0, "run_chat ran without a key")
        self.assertIn("Locked", [title for title, _ in calls["msgs"]])

    def test_about_is_reachable_without_key(self):
        self.assertEqual(self._run_menu(["3", "", "4"])["about"], 1)

    def test_configure_keys_option_is_reachable_without_key(self):
        calls = self._run_menu(["2", "3", "", "4"])
        self.assertEqual(calls["configure"], 1,
                         "option [2] never invoked configure_key()")

    def test_failed_verification_leaves_no_brain(self):
        """A brain that never authenticated must not survive for run_chat."""
        real_load_key = app_mod.App._load_key
        real_brain = app_mod.HexSecBrain
        self.addCleanup(setattr, app_mod.App, "_load_key", real_load_key)
        self.addCleanup(setattr, app_mod, "HexSecBrain", real_brain)
        # A syntactically invalid key, chosen so this file stays clean
        # under the repo's real-key secret scan.
        app_mod.App._load_key = lambda self: "invalid-test-key"

        def boom(*args, **kwargs):
            raise RuntimeError("401 unauthorized")

        class FakeBrain:
            def __init__(self, *args, **kwargs):
                self.client = type("Client", (), {
                    "models": type("Models", (), {"list": staticmethod(boom)})})()

        app_mod.HexSecBrain = FakeBrain
        app = app_mod.App()
        self.assertFalse(app.setup())
        self.assertIsNone(app.brain, "brain survived failed verification")

    def test_menu_shows_connection_status_row(self):
        from rich.console import Console
        for connected, expected in ((False, "No API Key"),
                                    (True, "Neural Link established")):
            with self.subTest(connected=connected):
                ui = app_mod.UI()
                ui.connected = connected
                buf = io.StringIO()
                real_console = ui.console
                ui.console = Console(file=buf, width=200, force_terminal=False,
                                     no_color=True, legacy_windows=False)
                try:
                    ui.main_menu()
                finally:
                    ui.console = real_console
                self.assertIn(expected, buf.getvalue())

    def test_valid_key_with_no_free_model_is_reported(self):
        """A key that verifies but resolves to 'auto' must say so, not chat."""
        msgs = []
        app = app_mod.App()
        real_show = app_mod.UI.show_msg
        self.addCleanup(setattr, app_mod.UI, "show_msg", real_show)
        app_mod.UI.show_msg = (
            lambda self, title, content, color="white": msgs.append((title, content)))

        class FakeBrain:
            def __init__(self, *a, **k):
                self.client = type("Client", (), {
                    "models": type("Models", (), {"list": staticmethod(lambda: None)})})()
                self.model = app_mod.Config.AUTO_MODEL

            def resolve_model(self):
                return app_mod.Config.AUTO_MODEL

        real_load_key = app_mod.App._load_key
        real_brain = app_mod.HexSecBrain
        real_sleep = app_mod.time.sleep
        self.addCleanup(setattr, app_mod.App, "_load_key", real_load_key)
        self.addCleanup(setattr, app_mod, "HexSecBrain", real_brain)
        self.addCleanup(setattr, app_mod.time, "sleep", real_sleep)
        app_mod.App._load_key = lambda self: "valid-key"
        app_mod.HexSecBrain = FakeBrain
        app_mod.time.sleep = lambda s: None

        self.assertTrue(app.setup())
        self.assertTrue(app._no_model)
        titles = [t for t, _ in msgs]
        self.assertIn("No Free Models", titles)
        self.assertNotIn("Model", titles, "printed a bogus active model")
        body = dict(msgs)["No Free Models"]
        self.assertIn("--list-models", body)

    def test_chat_explains_empty_free_tier_after_failed_switch(self):
        """Hitting a dead model must explain the empty tier, not fail mute."""
        msgs = []
        app = app_mod.App()
        real_show = app_mod.UI.show_msg
        real_banner = app_mod.UI.banner
        real_stream = app_mod.UI.stream_markdown
        real_get_input = app_mod.UI.get_input
        self.addCleanup(setattr, app_mod.UI, "show_msg", real_show)
        self.addCleanup(setattr, app_mod.UI, "banner", real_banner)
        self.addCleanup(setattr, app_mod.UI, "stream_markdown", real_stream)
        self.addCleanup(setattr, app_mod.UI, "get_input", real_get_input)
        app_mod.UI.banner = lambda self: None
        app_mod.UI.stream_markdown = lambda self, title, gen: list(gen)
        app_mod.UI.show_msg = (
            lambda self, title, content, color="white": msgs.append((title, content)))

        class DeadBrain:
            model = app_mod.Config.AUTO_MODEL

            def chat(self, prompt):
                yield "Error: Connection Terminated. Reason: model_not_found"

            def _switch_model(self, exclude):
                return False

        app.brain = DeadBrain()
        inputs = iter(["hello", "/exit"])
        app_mod.UI.get_input = lambda self, label="COMMAND": next(inputs)
        app.run_chat()
        titles = [t for t, _ in msgs]
        self.assertIn("No Free Models", titles,
                      "empty free tier was not explained after a dead model")
        self.assertIn("will not use a paid model", dict(msgs)["No Free Models"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
