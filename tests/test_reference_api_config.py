"""Synthetic provider files and mocked transport; no operator auth or API calls."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from auriga.core.config import Config
from auriga.apps.reference.reference_api import ResponsesClient
from scripts.reference_episode import main


class ReferenceApiConfigTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "selected"
        self.home.mkdir()
        self.config = Config.from_dict({"model": {"name": "gpt-6.1-sol", "effort": "high",
                                                 "codex_home": str(self.home)}})
        self.write_provider()
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic-selected"}')

    def write_provider(self, extra="", *, name="selected"):
        (self.home / "config.toml").write_text(
            f'model_provider="{name}"\nmodel="historical-home-model"\nmodel_reasoning_effort="low"\n'
            f'[model_providers.{name}]\nbase_url="https://selected.invalid/v1"\nwire_api="responses"\n' + extra)

    def test_factory_loads_new_defaults_and_only_selected_auth_home(self):
        with patch('auriga.core.config.LOCAL_CONFIG_PATH', self.root / 'absent.local.json'):
            defaults = Config.load(environ={})
        self.assertEqual((defaults.model.name, defaults.model.effort), ("gpt-6.1-sol", "high"))
        self.assertEqual(defaults.model.codex_home, str(Path("~/.codex").expanduser()))
        selected = defaults.with_overrides({"model": {"codex_home": str(self.home)}})
        with patch.object(Config, "load", return_value=selected) as load, patch.dict(os.environ, {
                "CODEX_HOME": str(self.root / "unselected"), "OPENAI_API_KEY": "synthetic-ambient"}, clear=True):
            client = ResponsesClient.from_codex_config(self.root / "calls")
        load.assert_called_once_with()
        self.assertEqual((client.model, client.effort, client._token),
                         ("gpt-6.1-sol", "high", "synthetic-selected"))
        self.assertEqual(client.runtime_provider["provider"], "selected")
        self.assertEqual(client.runtime_provider["codex_home"], str(self.home))
        self.assertNotIn("synthetic-selected", json.dumps(client.runtime_provider))

    def test_explicit_saved_config_and_constructor_overrides_preserve_history(self):
        legacy = self.config.with_overrides({"model": {"name": "gpt-6-astra", "effort": "medium"}})
        with patch.object(Config, "load", side_effect=AssertionError("unexpected reload")):
            client = ResponsesClient.from_codex_config(self.root / "calls", config=legacy)
            override = ResponsesClient.from_codex_config(self.root / "override", config=legacy,
                                                        model="gpt-6.1-sol", effort="high")
        self.assertEqual((client.model, client.effort), ("gpt-6-astra", "medium"))
        self.assertEqual((override.model, override.effort), ("gpt-6.1-sol", "high"))

    def test_explicit_home_override_never_searches_another_home(self):
        other = self.root / "other"
        other.mkdir()
        (other / "config.toml").write_text((self.home / "config.toml").read_text())
        (other / "auth.json").write_text('{"OPENAI_API_KEY":"synthetic-other"}')
        client = ResponsesClient.from_codex_config(self.root / "calls", config=self.config, codex_home=other)
        self.assertEqual(client._token, "synthetic-other")
        (other / "auth.json").unlink()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-ambient", "CODEX_HOME": str(self.home)}), \
                self.assertRaisesRegex(ValueError, "reference_provider_credential_unavailable"):
            ResponsesClient.from_codex_config(self.root / "missing", config=self.config, codex_home=other)

    def test_declared_env_key_and_provider_token_are_selected_explicitly(self):
        self.write_provider('env_key="SELECTED_SYNTHETIC_KEY"\nexperimental_bearer_token="synthetic-provider"\n')
        with patch.dict(os.environ, {"SELECTED_SYNTHETIC_KEY": "synthetic-env", "OPENAI_API_KEY": "synthetic-ambient"}, clear=True):
            client = ResponsesClient.from_codex_config(self.root / "env", config=self.config)
        self.assertEqual(client._token, "synthetic-env")
        with patch.dict(os.environ, {}, clear=True):
            client = ResponsesClient.from_codex_config(self.root / "provider", config=self.config)
        self.assertEqual(client._token, "synthetic-provider")

    def test_missing_custom_provider_or_endpoint_and_nonresponses_are_rejected(self):
        documents = (
            ('[model_providers.other]\nbase_url="https://other.invalid/v1"\n',
             "reference_selected_provider_unavailable"),
            ('model_provider=""\n', "reference_selected_provider_unavailable"),
            ('model_provider="absent"\n[model_providers.other]\nbase_url="https://other.invalid/v1"\n',
             "reference_selected_provider_unavailable:absent"),
            ('model_provider="fixture"\n[model_providers.fixture]\nwire_api="responses"\n',
             "reference_selected_provider_base_url_unavailable:fixture"),
            ('model_provider="fixture"\n[model_providers.fixture]\nwire_api="chat"\nbase_url="https://fixture.invalid/v1"\n',
             "reference_requires_responses_provider"),
        )
        for document, error in documents:
            with self.subTest(error=error):
                (self.home / "config.toml").write_text(document)
                with self.assertRaisesRegex(ValueError, error):
                    ResponsesClient.from_codex_config(self.root / "calls", config=self.config)

    def test_builtin_openai_uses_selected_auth_and_does_not_implicitly_use_env(self):
        (self.home / "config.toml").write_text('model_provider="openai"\n')
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-ambient"}, clear=True):
            client = ResponsesClient.from_codex_config(self.root / "calls", config=self.config)
        self.assertEqual(client.base_url, "https://api.openai.com/v1")
        self.assertEqual(client._token, "synthetic-selected")

    def test_direct_request_keeps_service_tier_separate_from_reasoning_effort(self):
        client = ResponsesClient("https://synthetic.invalid/v1", "synthetic-token", self.root / "direct")
        self.assertEqual((client.model, client.effort), ("gpt-6.1-sol", "high"))
        with patch.object(client, "_isolated_complete", return_value={"choices": []}):
            client.complete([], [])
        request = json.loads((self.root / "direct/call-001/request.json").read_text())
        self.assertEqual(request["service_tier"], "default")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(client.calls[0]["service_tier"], "default")
        self.assertNotIn("synthetic-token", json.dumps(client.calls))

    def test_cli_print_config_resolves_defaults_and_overrides_without_auth_or_workers(self):
        document = self.root / "machine.json"
        document.write_text(json.dumps({"model": {"name": "gpt-6-astra", "effort": "medium",
                                                  "codex_home": str(self.root / "legacy")}}))
        for arguments, expected in (
                (["--print-config"], ("gpt-6.1-sol", "high")),
                (["--config", str(document), "--print-config"], ("gpt-6-astra", "medium")),
                (["--config", str(document), "--model", "gpt-6.1-sol", "--effort", "high",
                  "--codex-home", str(self.home), "--print-config"], ("gpt-6.1-sol", "high"))):
            output = io.StringIO()
            with self.subTest(arguments=arguments), patch.dict(os.environ, {}, clear=True), \
                    contextlib.redirect_stdout(output), \
                    patch("scripts.reference_episode.upstream", side_effect=AssertionError("worker started")), \
                    patch.object(ResponsesClient, "from_codex_config", side_effect=AssertionError("auth read")):
                main(arguments)
            model = json.loads(output.getvalue())["model"]
            self.assertEqual((model["name"], model["effort"]), expected)
            if "--codex-home" in arguments:
                self.assertEqual(model["codex_home"], str(self.home))

    def test_episode_entrypoint_resolves_once_and_shares_config_with_client_and_backend(self):
        report = dict(loop_verified=True, episode_verified=True, termination_reason="synthetic")
        output = io.StringIO()
        with patch.object(Config, "load", return_value=self.config) as load, \
                patch("scripts.reference_episode.upstream"), \
                patch("scripts.reference_episode.register_run"), \
                patch("scripts.reference_episode.ReferenceRecorder") as recorder, \
                patch("scripts.reference_episode.RobotBackend") as backend, \
                patch("scripts.reference_episode.run_episode", return_value=report), \
                patch.object(ResponsesClient, "from_codex_config") as factory, \
                contextlib.redirect_stdout(output):
            main(["--run-dir", str(self.root / "episode"), "--gpu", "0", "--seed", "3", "--layout-id", "2"])
        load.assert_called_once_with(None, overrides={"model": {}})
        self.assertIs(factory.call_args.kwargs["config"], self.config)
        self.assertIs(backend.call_args.kwargs["config"], self.config)
        self.assertEqual(backend.call_args.kwargs["gpu_id"], 0)
        self.assertEqual(backend.call_args.kwargs["seed"], 3)
        backend.return_value.validate_reset.assert_called_once_with(3, 2)
        backend.return_value.close.assert_called_once_with()
        recorder.return_value.finish.assert_called_once_with(report)


if __name__ == "__main__":
    unittest.main()
