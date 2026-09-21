import json
import tempfile
import unittest
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

from agent.config import ModelSettings
from agent.runtime import semantic_config_digest
from agent.runtime.semantics import SEMANTIC_CONFIG_SCHEMA_VERSION
from tests.runtime._config_support import (
    RunConfigTestCase,
    model_settings,
)


class SemanticConfigDigestTests(RunConfigTestCase):
    def test_digest_schema_version_is_bumped_for_workspace_policy_semantics(self) -> None:
        self.assertEqual(SEMANTIC_CONFIG_SCHEMA_VERSION, 4)

    def test_digest_excludes_paths_and_api_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            first = self.make_config(Path(first_directory))
            rotated_key = replace(first, model=model_settings("rotated-secret"))
            moved_roots = self.make_config(Path(second_directory))

            self.assertEqual(
                semantic_config_digest(first),
                semantic_config_digest(rotated_key),
            )
            self.assertEqual(
                semantic_config_digest(first),
                semantic_config_digest(moved_roots),
            )
            self.assertNotIn(
                "secret-one", repr(semantic_config_digest(first))
            )
            serialized = json.dumps(
                asdict(first),
                default=str,
            )
            self.assertNotIn("secret-one", serialized)

    def test_digest_canonicalizes_equivalent_numeric_and_url_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            equivalent = replace(
                config,
                model=replace(
                    config.model,
                    base_url="https://API.DEEPSEEK.COM:443/",
                ),
                timeout_seconds=900,
                max_cost_usd=Decimal("2.5000"),
            )

            self.assertEqual(
                semantic_config_digest(config),
                semantic_config_digest(equivalent),
            )

    def test_every_semantic_change_changes_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)
            assert config.pricing is not None
            changed_specs = tuple(reversed(config.verification_specs))
            first_spec = config.verification_specs[0]
            second_spec = config.verification_specs[1]
            changes = {
                "provider": replace(
                    config,
                    model=ModelSettings(
                        "anthropic", "claude-sonnet-4-6", None, None
                    ),
                ),
                "model": replace(
                    config, model=replace(config.model, model="other")
                ),
                "endpoint": replace(
                    config,
                    model=replace(
                        config.model,
                        base_url="https://api.deepseek.com/v2",
                    ),
                ),
                "output_limit": replace(
                    config, model_max_output_tokens=8192
                ),
                "verification_order": replace(
                    config, verification_specs=changed_specs
                ),
                "verification_name": replace(
                    config,
                    verification_specs=(
                        replace(first_spec, name="renamed"),
                        second_spec,
                    ),
                ),
                "verification_argv": replace(
                    config,
                    verification_specs=(
                        replace(first_spec, argv=("python", "-V")),
                        second_spec,
                    ),
                ),
                "verification_cwd": replace(
                    config,
                    verification_specs=(
                        replace(first_spec, cwd="src"),
                        second_spec,
                    ),
                ),
                "verification_timeout": replace(
                    config,
                    verification_specs=(
                        replace(first_spec, timeout_seconds=31),
                        second_spec,
                    ),
                ),
                "verification_output_limit": replace(
                    config,
                    verification_specs=(
                        replace(first_spec, max_output_bytes=4097),
                        second_spec,
                    ),
                ),
                "verification_report_path": replace(
                    config,
                    verification_specs=(
                        replace(
                            first_spec,
                            argv=("python", "-m", "pytest", "--junitxml=report.xml"),
                            report_path="report.xml",
                        ),
                        second_spec,
                    ),
                ),
                "verification_allowed_failures": replace(
                    config,
                    verification_specs=(
                        replace(
                            first_spec,
                            allowed_failure_case_ids=("legacy",),
                        ),
                        second_spec,
                    ),
                ),
                "run_timeout": replace(config, timeout_seconds=901),
                "model_request_timeout": replace(
                    config, model_request_timeout_seconds=46
                ),
                "max_steps": replace(config, max_steps=41),
                "max_cost": replace(
                    config, max_cost_usd=Decimal("2.51")
                ),
                "pricing_source": replace(
                    config,
                    pricing=replace(config.pricing, source="new-source"),
                ),
                "input_pricing": replace(
                    config,
                    pricing=replace(
                        config.pricing,
                        input_cost_per_million_tokens=Decimal("0.26"),
                    ),
                ),
                "output_pricing": replace(
                    config,
                    pricing=replace(
                        config.pricing,
                        output_cost_per_million_tokens=Decimal("0.51"),
                    ),
                ),
                "pricing_presence": replace(config, pricing=None),
            }
            original = semantic_config_digest(config)

            for field, changed in changes.items():
                with self.subTest(field=field):
                    self.assertNotEqual(
                        semantic_config_digest(changed), original
                    )


if __name__ == "__main__":
    unittest.main()
