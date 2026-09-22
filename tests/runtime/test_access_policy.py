import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.constants import END, START
from langgraph.graph import StateGraph

from agent.architect.graph import create_architect_workflow
from agent.architect.models import ResearchEvaluation, ResearchStep
from agent.architect.runtime import ArchitectRuntime
from agent.common.entities import (
    AtomicTask,
    ImplementationPlan,
    ImplementationTask,
    PlanStatus,
)
from agent.developer.graph import create_developer_workflow
from agent.developer.runtime import DeveloperRuntime
from agent.developer.state import DeveloperStatus
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
from agent.graph import AgentState, create_production_workflow_graph
from agent.tools.codemap import (
    get_code_definitions,
    get_function_implementation,
    get_raw_file_content,
)
from agent.tools.search import search_keyword_in_directory
from agent.tools.write import get_files_structure
from agent.tools.write import get_files_structure
from agent.verification import VerificationCheckStatus, VerificationRunner, VerificationSpec
from agent.workspace import current_workspace_access_policy, workspace_root_scope
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

    def test_protected_missing_and_link_paths_are_indistinguishable(self) -> None:
        canary = "PROTECTED_METADATA_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "oracle").mkdir()
            (root / "oracle" / "existing.py").write_text(
                f"VALUE = {canary!r}\n", encoding="utf-8"
            )
            outside = root.parent / "outside-protected.py"
            outside.write_text(f"VALUE = {canary!r}\n", encoding="utf-8")
            link = root / "oracle" / "link.py"
            try:
                link.symlink_to(outside)
            except OSError as error:
                link = None
                symlink_skip = str(error)
            else:
                symlink_skip = None
            policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
            calls = (
                (get_raw_file_content, {"file_path": "oracle/existing.py"}),
                (get_code_definitions, {"file_path": "oracle/existing.py"}),
                (get_raw_file_content, {"file_path": "oracle/missing.py"}),
                (get_raw_file_content, {"file_path": "./oracle/missing.py"}),
                (
                    get_raw_file_content,
                    {"file_path": "workspace_repo/oracle/missing.py"},
                ),
                (get_code_definitions, {"file_path": "oracle/missing.py"}),
                (
                    search_keyword_in_directory,
                    {"directory": "oracle", "search_term": "VALUE"},
                ),
                (
                    search_keyword_in_directory,
                    {"directory": "oracle/missing-dir", "search_term": "VALUE"},
                ),
                (get_files_structure, {"directory": "oracle"}),
                (get_files_structure, {"directory": "oracle/missing-dir"}),
            )
            if link is not None:
                calls += ((get_raw_file_content, {"file_path": "oracle/link.py"}),)
            with workspace_root_scope(root, access_policy=policy):
                results = [tool.invoke(arguments) for tool, arguments in calls]
            for result in results:
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error_code"], "read_denied", result)
                self.assertIsNone(result["path"], result)
                self.assertNotIn("oracle", json.dumps(result))
                self.assertNotIn(canary, json.dumps(result))
            if symlink_skip is not None:
                self.assertTrue(symlink_skip)

            with workspace_root_scope(root, access_policy=policy):
                outside_result = get_raw_file_content.invoke(
                    {"file_path": str(outside)}
                )
            self.assertFalse(outside_result["ok"])
            self.assertEqual(outside_result["error_code"], "path_invalid")

    def test_compiled_architect_and_developer_tool_nodes_keep_policy_context(self) -> None:
        canary = "COMPILED_POLICY_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "oracle").mkdir()
            (root / "oracle" / "expected.txt").write_text(
                canary, encoding="utf-8"
            )
            (root / "target.py").write_text("value = 1\n", encoding="utf-8")
            policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
            protected_tools = (
                get_raw_file_content,
                get_code_definitions,
                search_keyword_in_directory,
                get_files_structure,
            )
            protected_tool_calls = [
                {
                    "name": "get_raw_file_content",
                    "args": {"file_path": "oracle/expected.txt"},
                    "id": "policy-read-call",
                    "type": "tool_call",
                },
                {
                    "name": "get_code_definitions",
                    "args": {"file_path": "oracle/expected.txt"},
                    "id": "policy-codemap-call",
                    "type": "tool_call",
                },
                {
                    "name": "search_keyword_in_directory",
                    "args": {"directory": "oracle", "search_term": "VALUE"},
                    "id": "policy-search-call",
                    "type": "tool_call",
                },
                {
                    "name": "get_files_structure",
                    "args": {"directory": "oracle"},
                    "id": "policy-tree-call",
                    "type": "tool_call",
                },
            ]
            architect_calls = 0
            architect_tool_results = []

            def architect_conduct(_values):
                nonlocal architect_calls
                architect_calls += 1
                if architect_calls == 1:
                    return AIMessage(
                        content="",
                        tool_calls=protected_tool_calls,
                    )
                architect_tool_results.extend(
                    message
                    for message in _values["implementation_research_scratchpad"]
                    if "UNTRUSTED EVIDENCE" in str(message.content)
                )
                return AIMessage(content="No more research.")

            architect_runtime = ArchitectRuntime(
                plan_next_step=lambda _values: ResearchStep(
                    reasoning="inspect", hypothesis="inspect"
                ),
                check_research_step=lambda _values: ResearchEvaluation(
                    reasoning="valid", is_valid=True
                ),
                conduct_research=architect_conduct,
                extract_implementation_plan=lambda _values: ImplementationPlan(
                    status=PlanStatus.NO_CHANGES,
                    no_change_reason="No changes needed.",
                    tasks=[],
                ),
                load_codebase_structure=lambda: "target.py",
            )
            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                architect_result = create_architect_workflow(
                    architect_runtime,
                    research_tools=protected_tools,
                    access_policy=policy,
                ).invoke({"implementation_research_scratchpad": []})
            self.assertTrue(architect_result["implementation_plan"] is not None)
            self.assertTrue(architect_tool_results)
            architect_tool_text = json.dumps(
                [message.content for message in architect_tool_results]
            )
            self.assertNotIn(canary, architect_tool_text)
            self.assertNotIn("oracle", architect_tool_text)
            self.assertNotIn("expected.txt", architect_tool_text)

            developer_calls = 0
            developer_tool_results = []
            plan = ImplementationPlan(
                tasks=[
                    ImplementationTask(
                        file_path="target.py",
                        logical_task="update value",
                        atomic_tasks=[AtomicTask(atomic_task="update value")],
                    )
                ]
            )
            editor = WorkspaceEditor(root, access_policy=policy)

            def developer_research(_values):
                nonlocal developer_calls
                developer_calls += 1
                if developer_calls == 1:
                    return AIMessage(
                        content="",
                        tool_calls=protected_tool_calls,
                    )
                developer_tool_results.extend(
                    message
                    for message in _values["atomic_implementation_research"]
                    if "UNTRUSTED EVIDENCE" in str(message.content)
                )
                return AIMessage(content="Research complete.")

            developer_runtime = DeveloperRuntime(
                edit_executor=lambda: DeveloperEditExecutor(editor),
                load_codebase_structure=lambda: "target.py",
                research_atomic_task=developer_research,
                propose_existing_file_edit=lambda _values: (
                    "<<<<<<< SEARCH\nvalue = 1\n=======\n"
                    "value = 2\n>>>>>>> REPLACE"
                ),
                propose_new_file=lambda _values: "value = 2\n",
            )
            with patch.dict(
                os.environ, {"SWE_AGENT_WORKSPACE": str(root)}, clear=False
            ):
                developer_result = create_developer_workflow(
                    developer_runtime,
                    research_tools=protected_tools,
                    access_policy=policy,
                ).invoke({"implementation_plan": plan})
            self.assertTrue(developer_result["developer_status"])
            self.assertTrue(developer_tool_results)
            developer_tool_text = json.dumps(
                [message.content for message in developer_tool_results]
            )
            self.assertNotIn(canary, developer_tool_text)
            self.assertNotIn("oracle", developer_tool_text)
            self.assertNotIn("expected.txt", developer_tool_text)
            self.assertEqual((root / "target.py").read_text(), "value = 2\n")

    def test_parent_production_graph_invokes_compiled_children_inside_policy_scope(
        self,
    ) -> None:
        policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
        observed_policies = []

        def compiled_child(role: str):
            child = StateGraph(AgentState)

            def invoke(state: AgentState):
                observed_policies.append(current_workspace_access_policy())
                if role == "architect":
                    return {
                        "implementation_plan": ImplementationPlan(
                            status=PlanStatus.NO_CHANGES,
                            no_change_reason="No changes needed.",
                            tasks=[],
                        )
                    }
                return {"developer_status": DeveloperStatus.NO_CHANGES}

            child.add_node("invoke", invoke)
            child.add_edge(START, "invoke")
            child.add_edge("invoke", END)
            return child.compile()

        architect = compiled_child("architect")
        developer = compiled_child("developer")
        with patch.dict(
            os.environ, {"SWE_AGENT_VERIFICATION_CHECKS": ""}, clear=False
        ):
            with patch("agent.graph.swe_architect", architect), patch(
                "agent.graph.swe_developer", developer
            ):
                result = create_production_workflow_graph(
                    access_policy=policy
                ).compile().invoke({})

        self.assertEqual(result["developer_status"], DeveloperStatus.NO_CHANGES)
        self.assertEqual(result["outcome"], "no_changes")
        self.assertEqual(observed_policies, [policy, policy])


if __name__ == "__main__":
    unittest.main()
