import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent.editing import EditOperation, EditProposal, EditStatus, WorkspaceEditor
from agent.developer.editing import DeveloperEditExecutor
from agent.runtime import (
    AgentCodeRevision,
    AgentRevisionStatus,
    RunCheckpoint,
    ResumeRequest,
    RunIdentity,
    WorkspaceAccessPolicy,
    WorkspaceIdentity,
    preflight_resume,
    semantic_config_digest,
)
from agent.tools.codemap import (
    get_code_definitions,
    get_function_implementation,
    get_raw_file_content,
)
from agent.tools.search import search_keyword_in_directory
from agent.tools.write import get_files_structure
from agent.tools.write import get_files_structure
from agent.verification import VerificationCheckStatus, VerificationRunner, VerificationSpec
from agent.workspace import workspace_root_scope
from tests.runtime._config_support import RunConfigTestCase


class WorkspaceAccessPolicyTests(RunConfigTestCase):
    def test_read_and_write_denials_share_canonical_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret", encoding="utf-8")
            (root / "hidden").mkdir()
            (root / "hidden" / "source.py").write_text(
                "def hidden_function():\n    return 'canary_hidden'\n",
                encoding="utf-8",
            )
            (root / "oracle").mkdir()
            (root / "oracle" / "expected.txt").write_text(
                "oracle-value", encoding="utf-8"
            )
            policy = WorkspaceAccessPolicy(
                hidden_paths=("hidden",), oracle_paths=("oracle",)
            )
            editor = WorkspaceEditor(root, access_policy=policy)

            snapshot = editor.snapshot("hidden/source.py")
            started = editor.begin("oracle/expected.txt")
            self.assertEqual(snapshot.error_code.value, "read_denied")
            self.assertEqual(started.edit_result.error_code.value, "write_denied")

            with workspace_root_scope(root, access_policy=policy):
                tree = get_files_structure.invoke({"directory": "."})
                search = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "canary"}
                )
                direct = get_raw_file_content.invoke({"file_path": ".git/config"})
                definitions = get_code_definitions.invoke(
                    {"file_path": "hidden/source.py"}
                )
                implementation = get_function_implementation.invoke(
                    {
                        "file_path": "hidden/source.py",
                        "function_name": "hidden_function",
                    }
                )
            rendered = json.dumps(
                (tree, search, direct, definitions, implementation),
                ensure_ascii=False,
            )
            self.assertNotIn("hidden", rendered)
            self.assertNotIn("source.py", rendered)
            self.assertNotIn("oracle", rendered)
            self.assertNotIn("expected.txt", rendered)
            self.assertNotIn("canary_hidden", rendered)
            self.assertEqual(direct["error_code"], "read_denied")
            self.assertEqual(definitions["error_code"], "read_denied")
            self.assertEqual(implementation["error_code"], "read_denied")

    def test_plan_target_and_commit_are_write_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("API_KEY=secret", encoding="utf-8")
            editor = WorkspaceEditor(root, access_policy=WorkspaceAccessPolicy())
            result = editor.apply(
                EditProposal(
                    task_id="task",
                    path=".env",
                    operation=EditOperation.EDIT,
                    old_text="API_KEY=secret",
                    new_text="API_KEY=changed",
                )
            )
            self.assertEqual(result.status, EditStatus.REJECTED)
            self.assertEqual(result.error_code.value, "write_denied")
            self.assertEqual((root / ".env").read_text(), "API_KEY=secret")

    def test_trusted_verification_can_read_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "oracle").mkdir()
            (root / "oracle" / "expected.txt").write_text("ok", encoding="utf-8")
            spec = VerificationSpec(
                name="oracle",
                argv=(
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('oracle/expected.txt').read_text() == 'ok'",
                ),
            )
            with workspace_root_scope(
                root,
                access_policy=WorkspaceAccessPolicy(oracle_paths=("oracle",)),
            ):
                result = VerificationRunner(root).run(spec)
            self.assertEqual(result.status, VerificationCheckStatus.PASS)

    def test_policy_is_semantic_and_changes_resume_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_config(
                root,
                access_policy=WorkspaceAccessPolicy(
                    hidden_paths=("private", "alpha"), oracle_paths=("oracle",)
                ),
            )
            changed = self.make_config(
                root,
                access_policy=WorkspaceAccessPolicy(hidden_paths=("other",)),
            )
            self.assertNotEqual(
                semantic_config_digest(first), semantic_config_digest(changed)
            )
            reordered = self.make_config(
                root,
                access_policy=WorkspaceAccessPolicy(
                    hidden_paths=("alpha", "private"), oracle_paths=("oracle",)
                ),
            )
            self.assertEqual(
                semantic_config_digest(first), semantic_config_digest(reordered)
            )

            aliases = (
                WorkspaceAccessPolicy(oracle_paths=("oracle",)),
                WorkspaceAccessPolicy(oracle_paths=("./oracle",)),
                WorkspaceAccessPolicy(oracle_paths=("workspace_repo/oracle",)),
            )
            self.assertEqual(aliases[0], aliases[1])
            self.assertEqual(aliases[0], aliases[2])
            self.assertEqual(
                semantic_config_digest(
                    self.make_config(root, access_policy=aliases[0])
                ),
                semantic_config_digest(
                    self.make_config(root, access_policy=aliases[2])
                ),
            )

    def test_resume_rejects_policy_change_before_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_config(
                root,
                access_policy=WorkspaceAccessPolicy(hidden_paths=("private",)),
            )
            changed = self.make_config(
                root,
                access_policy=WorkspaceAccessPolicy(hidden_paths=("other",)),
            )
            identity = RunIdentity(
                "run", "thread", "task", WorkspaceIdentity.from_root(first.workspace_root)
            )
            revision = AgentCodeRevision("a" * 40, AgentRevisionStatus.KNOWN)
            checkpoint = RunCheckpoint(
                identity, semantic_config_digest(first), revision
            )
            result = preflight_resume(
                ResumeRequest(identity, semantic_config_digest(changed), revision),
                type("Lookup", (), {"get": lambda self, _: checkpoint})(),
            )
            self.assertEqual(result.error_code.value, "run_config_mismatch")

    def test_hard_link_aliases_are_denied_without_leaking_content(self) -> None:
        canary = "HARDLINK_PROTECTED_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / "oracle").mkdir()
            protected = {
                root / ".git" / "config": root / "public_git.py",
                root / "credentials": root / "public_credentials.py",
                root / "oracle" / "expected.py": root / "public_oracle.py",
            }
            for source, alias in protected.items():
                source.write_text(canary, encoding="utf-8")
                try:
                    alias.hardlink_to(source)
                except OSError as error:
                    self.skipTest(f"hard links are unavailable: {error}")
            before = {
                source: (source.read_bytes(), source.stat().st_mtime_ns)
                for source in protected
            }
            policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
            editor = WorkspaceEditor(root, access_policy=policy)
            developer = DeveloperEditExecutor(editor)

            with workspace_root_scope(root, access_policy=policy):
                raw = get_raw_file_content.invoke({"file_path": "public_git.py"})
                definitions = get_code_definitions.invoke(
                    {"file_path": "public_oracle.py"}
                )
                search = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "HARDLINK"}
                )
                tree = get_files_structure.invoke({"directory": "."})
            snapshot = editor.snapshot("public_oracle.py")
            started = editor.begin("public_oracle.py")
            checked = editor.check_write("public_oracle.py")
            developer_snapshot = developer.prepare(
                "./workspace_repo/public_oracle.py"
            )
            developer_check = developer.check_write(
                "./workspace_repo/public_oracle.py"
            )

            rendered = json.dumps((raw, definitions, search, tree))
            self.assertNotIn(canary, rendered)
            self.assertNotIn("public_git.py", rendered)
            self.assertNotIn("public_oracle.py", rendered)
            self.assertEqual(raw["error_code"], "read_denied")
            self.assertEqual(definitions["error_code"], "read_denied")
            self.assertEqual(snapshot.error_code.value, "read_denied")
            self.assertEqual(started.edit_result.error_code.value, "write_denied")
            self.assertEqual(checked.error_code.value, "write_denied")
            self.assertEqual(developer_snapshot.error_code.value, "read_denied")
            self.assertEqual(developer_check.error_code.value, "write_denied")
            for source, (content, mtime_ns) in before.items():
                self.assertEqual(source.read_bytes(), content)
                self.assertEqual(source.stat().st_mtime_ns, mtime_ns)


if __name__ == "__main__":
    unittest.main()
