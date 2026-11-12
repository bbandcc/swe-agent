"""Supported CI selection and its contract; native history is retained separately."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_TARGETS = (
    'tests.architect.test_workflow',
    'tests.common.test_tool_message_renderer',
    'tests.developer.test_edit_executor',
    'tests.developer.test_workflow',
    'tests.editing.test_workspace_editor',
    'tests.editing.test_s1_demo',
    'tests.editing.test_write_recovery',
    'tests.evaluation.test_isolated_forbiddenfruit_build.PlatformContractTests.test_non_windows_never_dispatches_or_publishes',
    'tests.evaluation.test_isolated_source_build.IsolatedSourceBuildTests.test_non_windows_has_no_fallback_or_artifact',
    'tests.evaluation.test_isolated_source_build.CanaryInputTests.test_explicit_local_pin_uses_no_network',
    'tests.evaluation.test_isolated_source_build.CanaryInputTests.test_missing_or_tampered_local_pin_cannot_download_a_fallback',
    'tests.evaluation.test_python_toolchain',
    'tests.evaluation.test_python_toolchain_boundary',
    # Real Docker controls stay opt-in; these are bounded offline seams.
    'tests.evaluation.test_pilot_control',
    'tests.evaluation.test_model_pilot',
    'tests.evaluation.test_pilot_model_protocol',
    'tests.evaluation.test_pilot_execution.PilotExecutionAdmissionTests',
    'tests.evaluation.test_pilot_spend',
    'tests.evaluation.test_pilot_tasks.PilotTaskTests',
    'tests.evaluation.test_pilot_workflow.PilotAssemblyTests',
    'tests.evaluation.test_task_execution.TaskExecutionTests.test_hidden_path_canary_command_writes_only_when_paths_are_absent',
    'tests.evaluation.test_task_execution.TaskExecutionTests.test_invalid_policy_fails_before_copy_or_sandbox_dispatch',
    'tests.evaluation.test_task_manifest',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_default_policy_is_explicit_and_omission_is_rejected',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_invalid_policy_is_structured_before_workspace_copy',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_pinned_task_copies_are_independent_and_exclude_oracle_and_git',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_trusted_hidden_and_oracle_paths_are_removed_before_execution',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_public_sibling_of_oracle_remains_available_to_python_checks',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_edit_scope_conflicting_with_trusted_protection_rejects_before_copy',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_patch_is_collected_from_task_tree_with_hash_and_size_evidence',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_out_of_scope_change_is_rejected_without_patch_success',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_invalid_revision_and_scope_fail_closed',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_root_inside_baseline_repository_is_rejected',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_root_equal_to_repository_is_rejected',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_repository_nested_under_runtime_root_is_rejected',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_root_symlink_is_rejected_without_following_target',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_root_junction_is_rejected_without_following_target',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_below_linked_ancestor_is_canonicalized_and_created',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_runtime_below_junction_ancestor_is_canonicalized_and_created',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_oracle_scope_must_not_be_empty',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_prepare_reports_cleanup_failure_instead_of_hiding_it',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_workspace_cleanup_failure_is_structured_and_retryable',
    'tests.evaluation.test_task_workspace.TaskWorkspaceTests.test_cleanup_is_explicit_and_idempotent',
    'tests.evaluation.test_windows_preparation_withdrawal',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_public_valid_request_refused_before_resources',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_refusal_fields_have_no_execution_or_resource_usage',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_invalid_request_limits_keep_priority_over_workspace',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_invalid_workspace_precedes_cwd_and_executable',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_cwd_executable_and_toolchain_validation_are_not_bypassed',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_valid_owned_sibling_toolchain_still_reaches_profile_refusal',
    'tests.evaluation.test_windows_profile_withdrawal.WindowsProfileWithdrawalTests.test_direct_backend_refused_before_resources',
    'tests.evaluation.test_windows_profile_withdrawal.UnsupportedPlatformWithdrawalTests.test_non_windows_precedes_invalid_input_without_backend_import',
    'tests.evaluation.test_windows_sandbox.WindowsAppContainerIntegrationTests.test_invalid_workspace_is_structured',
    'tests.evaluation.test_windows_sandbox.WindowsAppContainerIntegrationTests.test_invalid_python_toolchain_path_is_structured_before_profile',
    'tests.evaluation.test_windows_sandbox.UnsupportedSandboxPlatformTests.test_non_windows_backend_fails_closed',
    'tests.runtime.test_access_policy',
    'tests.runtime.test_admission',
    'tests.runtime.test_artifacts_runtime',
    'tests.runtime.test_budget',
    'tests.runtime.test_budgeted_graphs',
    'tests.runtime.test_config_loading',
    'tests.runtime.test_config_semantics',
    'tests.runtime.test_config_validation',
    'tests.runtime.test_durable_runtime',
    'tests.runtime.test_identity',
    'tests.runtime.test_model_timeout',
    'tests.runtime.test_model_usage',
    'tests.runtime.test_runtime_injection',
    'tests.runtime.test_run_summary',
    'tests.runtime.test_secret_final_seal',
    'tests.runtime.test_secrets',
    'tests.runtime.test_trajectory',
    'tests.runtime.test_verification_recovery',
    'tests.runtime.test_workspace_revision',
    'tests.runtime.test_write_recovery',
    'tests.test_config',
    'tests.test_graph_integration',
    'tests.test_s5d1_installation',
    'tests.test_s5d2_ci_contract',
    'tests.test_sdist_review_evidence',
    'tests.tools.test_bounded_reads',
    'tests.tools.test_javascript_symbols',
    'tests.tools.test_python_symbols',
    'tests.tools.test_tsx_symbols',
    'tests.tools.test_typescript_symbols',
    'tests.tools.test_workspace_boundary',
    'tests.verification.test_config',
    'tests.verification.test_evaluation',
    'tests.verification.test_evidence',
    'tests.verification.test_host_admission',
    'tests.verification.test_public_imports',
    'tests.verification.test_runner',
    'tests.verification.test_workflow',
)

def run_supported(suite):
    if suite == "unittest":
        command = [sys.executable, "-m", "unittest", "-v", *SUPPORTED_TARGETS]
    elif suite == "pytest":
        nodes = []
        for target in SUPPORTED_TARGETS:
            parts = target.split(".")
            index = next(i for i, part in enumerate(parts) if part.startswith("test_"))
            file = "/".join(parts[:index + 1]) + ".py"
            nodes.append(file + "".join("::" + p for p in parts[index + 1:]))
        command = [sys.executable, "-m", "pytest", "-rs", *nodes]
    else:
        raise ValueError("Only unittest or pytest is supported")
    return subprocess.run(command, cwd=ROOT, check=False).returncode


class ContinuousIntegrationContractTests(unittest.TestCase):
    def test_readme_and_contributing_share_the_supported_development_gates(self):
        for name in ("README.md", "CONTRIBUTING.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            for fragment in (
                "Python 3.12 (the locked development and CI baseline)",
                "uv sync --locked",
                "uv run --locked python -m tests.test_s5d2_ci_contract --suite unittest",
                "uv run --locked python -m tests.test_s5d2_ci_contract --suite pytest",
                "uv run --locked python -m compileall -q agent tests",
                "Windows AppContainer execution is withdrawn",
                "UNVERIFIED", "reviewed_files", "not a sandbox",
            ):
                with self.subTest(document=name, fragment=fragment):
                    self.assertIn(fragment, text)
            self.assertNotIn("unittest discover", text)

    def test_ci_workflow_keeps_locked_cross_platform_project_gates(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for fragment in (
            "permissions:\n  contents: read", "os: [ubuntu-latest, windows-latest]",
            "fetch-depth: 0", "persist-credentials: false", 'python-version: "3.12"',
            'version: "0.12.1"', "uv sync --locked",
            "uv run --locked python -m tests.test_s5d2_ci_contract --suite unittest",
            "uv run --locked python -m tests.test_s5d2_ci_contract --suite pytest",
            "uv run --locked python -m compileall -q agent tests",
        ):
            with self.subTest(fragment=fragment):self.assertIn(fragment, text)
        self.assertRegex(text, r"uses:\s*astral-sh/setup-uv@[0-9a-f]{40}")
        self.assertNotIn("unittest discover", text)
        self.assertNotIn("python -m pytest\n", text)
        self.assertNotIn("permissions: write-all", text)
        self.assertNotIn("secrets.", text)
        self.assertEqual(len(SUPPORTED_TARGETS), len(set(SUPPORTED_TARGETS)))
        for target in SUPPORTED_TARGETS:
            self.assertTrue(target.startswith("tests."))
            self.assertNotIn(".RealIsolatedBuildTests", target)
            self.assertNotIn(".RealForbiddenfruitBuildTests", target)
            self.assertNotIn("test_task_consumer_keeps_identity_failure_and_cleanup_priority", target)
        for module in ("tests.runtime.test_identity", "tests.runtime.test_admission",
                       "tests.runtime.test_access_policy", "tests.editing.test_workspace_editor",
                       "tests.tools.test_workspace_boundary", "tests.verification.test_host_admission",
                       "tests.evaluation.test_python_toolchain"):
            self.assertIn(module, SUPPORTED_TARGETS)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--suite":
        raise SystemExit(run_supported(sys.argv[2]))
    unittest.main()
