import math
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from agent.runtime import (
    RunConfigError,
    RunConfigErrorCode,
    TokenPricing,
    semantic_config_digest,
)
from agent.workspace import WorkspaceAccessPolicy
from agent.verification import VerificationSpec
from tests.runtime._config_support import (
    RunConfigTestCase,
    model_settings,
    verification_spec,
)


class RunConfigValidationTests(RunConfigTestCase):
    def test_rejects_unsafe_access_policy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("../outside", "/absolute", "C:\\outside"):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        WorkspaceAccessPolicy(hidden_paths=(path,))

    def test_constructs_one_canonical_validated_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self.make_config(root)

            self.assertTrue(config.workspace_root.is_absolute())
            self.assertTrue(config.runtime_root.is_absolute())
            self.assertEqual(config.model.base_url, "https://api.deepseek.com")
            self.assertEqual(config.max_cost_usd, Decimal("2.50"))

    def test_copies_mutable_verification_argv_into_digest_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = ["python", "-m", "unittest"]
            spec = VerificationSpec(name="unit", argv=argv)  # type: ignore[arg-type]
            config = self.make_config(root, verification_specs=[spec])
            digest = semantic_config_digest(config)

            argv.append("discover")

            self.assertIsInstance(config.verification_specs, tuple)
            self.assertIsInstance(config.verification_specs[0].argv, tuple)
            self.assertEqual(semantic_config_digest(config), digest)

    def test_rejects_non_finite_or_non_positive_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                ("model_max_output_tokens", 0),
                ("model_max_output_tokens", -1),
                ("model_max_output_tokens", True),
                ("timeout_seconds", 0),
                ("timeout_seconds", -1),
                ("timeout_seconds", math.nan),
                ("timeout_seconds", math.inf),
                ("model_request_timeout_seconds", 0),
                ("model_request_timeout_seconds", -1),
                ("model_request_timeout_seconds", math.nan),
                ("model_request_timeout_seconds", math.inf),
                ("max_steps", 0),
                ("max_steps", -1),
                ("max_steps", True),
                ("max_cost_usd", Decimal("0")),
                ("max_cost_usd", Decimal("-1")),
                ("max_cost_usd", Decimal("NaN")),
                ("max_cost_usd", Decimal("Infinity")),
            )
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(RunConfigError) as raised:
                        self.make_config(root, **{field: value})
                    self.assertEqual(
                        raised.exception.code, RunConfigErrorCode.INVALID_VALUE
                    )

    def test_retry_policy_is_single_attempt_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RunConfigError):
                self.make_config(
                    Path(directory),
                    model_retry_policy={"max_attempts": 2},
                )

    def test_rejects_invalid_pricing(self) -> None:
        cases = (
            (Decimal("0"), Decimal("1"), "source"),
            (Decimal("1"), Decimal("-1"), "source"),
            (Decimal("NaN"), Decimal("1"), "source"),
            (Decimal("1"), Decimal("Infinity"), "source"),
            (Decimal("1"), Decimal("1"), ""),
        )
        for input_price, output_price, source in cases:
            with self.subTest(
                input_price=input_price,
                output_price=output_price,
                source=source,
            ):
                with self.assertRaises(RunConfigError):
                    TokenPricing(input_price, output_price, source)

    def test_rejects_invalidmodel_settings_base_urls(self) -> None:
        invalid_urls = (
            "api.deepseek.com",
            "ftp://api.deepseek.com",
            "https://user:password@api.deepseek.com",
            "https://api.deepseek.com?api_key=secret",
            "https://api.deepseek.com#fragment",
            "https:///missing-host",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for base_url in invalid_urls:
                with self.subTest(base_url=base_url):
                    with self.assertRaises(RunConfigError) as raised:
                        self.make_config(
                            root,
                            model=replace(model_settings(), base_url=base_url),
                        )
                    self.assertEqual(
                        raised.exception.code,
                        RunConfigErrorCode.INVALID_BASE_URL,
                    )

    def test_rejects_plaintext_api_key_in_run_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RunConfigError) as raised:
                self.make_config(
                    root,
                    model=replace(model_settings(), api_key="plain-secret"),
                )

            self.assertEqual(
                raised.exception.code, RunConfigErrorCode.INVALID_VALUE
            )

    def test_rejects_overlapping_workspace_and_runtime_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cases = (
                workspace,
                workspace / "runtime",
                root,
            )
            for runtime in cases:
                with self.subTest(runtime=runtime):
                    with self.assertRaises(RunConfigError) as raised:
                        self.make_config(
                            root,
                            workspace_root=workspace,
                            runtime_root=runtime,
                        )
                    self.assertEqual(
                        raised.exception.code,
                        RunConfigErrorCode.ROOTS_OVERLAP,
                    )

    def test_rejects_missing_workspace_and_file_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_file = root / "runtime-file"
            runtime_file.write_text("not a directory", encoding="utf-8")
            cases = (
                {"workspace_root": root / "missing-workspace"},
                {"runtime_root": runtime_file},
            )
            for changes in cases:
                with self.subTest(changes=changes):
                    with self.assertRaises(RunConfigError) as raised:
                        self.make_config(root, **changes)
                    self.assertEqual(
                        raised.exception.code,
                        RunConfigErrorCode.INVALID_ROOT,
                    )

    def test_rejects_symbolic_link_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_workspace = root / "real-workspace"
            real_workspace.mkdir()
            link = root / "workspace-link"
            try:
                link.symlink_to(real_workspace, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            with self.assertRaises(RunConfigError) as raised:
                self.make_config(root, workspace_root=link)

            self.assertEqual(
                raised.exception.code, RunConfigErrorCode.INVALID_ROOT
            )

    def test_canonicalizes_root_below_symbolic_link_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_parent = root / "real-parent"
            real_parent.mkdir()
            link = root / "linked-parent"
            try:
                link.symlink_to(real_parent, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            config = self.make_config(root, runtime_root=link / "runtime")

            self.assertEqual(
                config.runtime_root,
                (real_parent / "runtime").resolve(),
            )

    @unittest.skipUnless(os.name == "nt", "junctions are a Windows path type")
    def test_rejects_junction_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_runtime = root / "real-runtime"
            real_runtime.mkdir()
            junction = root / "runtime-junction"
            completed = subprocess.run(
                [
                    "pwsh.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-CommandWithArgs",
                    "New-Item -ItemType Junction "
                    "-Path $args[0] -Target $args[1] | Out-Null",
                    str(junction),
                    str(real_runtime),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(
                    f"junction creation unavailable: {completed.stderr}"
                )

            with self.assertRaises(RunConfigError) as raised:
                self.make_config(root, runtime_root=junction)

            self.assertEqual(
                raised.exception.code, RunConfigErrorCode.INVALID_ROOT
            )

    def test_rejects_invalid_direct_verification_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                replace(verification_spec("unit", "unittest"), name=""),
                replace(
                    verification_spec("unit", "unittest"),
                    name=1,  # type: ignore[arg-type]
                ),
                replace(verification_spec("unit", "unittest"), argv=()),
                replace(
                    verification_spec("unit", "unittest"),
                    argv="python",  # type: ignore[arg-type]
                ),
                replace(
                    verification_spec("unit", "unittest"),
                    cwd="bad\x00path",
                ),
                replace(
                    verification_spec("unit", "unittest"),
                    cwd=1,  # type: ignore[arg-type]
                ),
                replace(
                    verification_spec("unit", "unittest"),
                    max_output_bytes=0,
                ),
            )
            for spec in cases:
                with self.subTest(spec=spec):
                    with self.assertRaises(RunConfigError):
                        self.make_config(root, verification_specs=(spec,))

            with self.assertRaises(RunConfigError):
                self.make_config(root, verification_specs=None)

    def test_rejects_duplicate_verification_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = verification_spec("unit", "compileall")
            with self.assertRaisesRegex(RunConfigError, "duplicate"):
                self.make_config(
                    root,
                    verification_specs=(
                        verification_spec("unit", "unittest"),
                        duplicate,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
