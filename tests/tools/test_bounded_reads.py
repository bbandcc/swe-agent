import json
import hashlib
import subprocess
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from agent.developer.state import SoftwareDeveloperState
from agent.architect.state import SoftwareArchitectState
from agent.editing import EditOperation, EditProposal, WorkspaceEditor

from agent.tools.codemap import get_raw_file_content
from agent.tools.read_contract import (
    MAX_CURSOR_CHARS,
    MAX_RAW_PAGE_BYTES,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_SCAN_ENTRIES,
    MAX_TREE_SCAN_ENTRIES,
    encode_cursor,
)
from agent.tools.search import search_keyword_in_directory
from agent.tools.write import get_files_structure
from agent.workspace import WorkspaceAccessPolicy, workspace_root_scope


class BoundedReadToolTests(unittest.TestCase):
    def test_raw_cursor_cannot_be_reused_for_another_target(self) -> None:
        content = b"a" * (MAX_RAW_PAGE_BYTES + 10)
        (self.root / "first.txt").write_bytes(content)
        (self.root / "second.txt").write_bytes(content)
        first = get_raw_file_content.invoke({"file_path": "first.txt"})
        self.assertTrue(first["ok"], first)
        self.assertIsNotNone(first["continuation"])
        other = get_raw_file_content.invoke({
            "file_path": "second.txt", "cursor": first["continuation"],
        })
        self.assertFalse(other["ok"], other)
        self.assertEqual(other["error_code"], "stale_cursor")
        self.assertNotIn("content", other)

    def test_search_cursor_fails_closed_when_encoded_token_exceeds_limit(self) -> None:
        for index in range(101):
            (self.root / f"f{index:03}.py").write_text("needle\n", encoding="utf-8")
        with patch("agent.tools.search.MAX_CURSOR_CHARS", 1):
            result = search_keyword_in_directory.invoke({
                "directory": ".", "search_term": "needle", "context": 0,
            })
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["continuation"])
        self.assertIn("cursor_size_limit_reached", result["warnings"])

    def test_search_cursor_fails_closed_at_directory_evidence_capacity(self) -> None:
        for index in range(MAX_SEARCH_FILES + 2):
            (self.root / f"a{index:03}").mkdir()
        (self.root / "z").mkdir()
        for index in range(101):
            (self.root / "z" / f"f{index:03}.py").write_text("needle\n", encoding="utf-8")
        result = search_keyword_in_directory.invoke({
            "directory": ".", "search_term": "needle", "context": 0,
        })
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["continuation"])
        self.assertIn("cursor_directory_evidence_incomplete", result["warnings"])

    def raw_in_developer(self, transaction, **args):
        builder = StateGraph(SoftwareDeveloperState)
        builder.add_node("tools", ToolNode(
            [get_raw_file_content], messages_key="atomic_implementation_research"
        ))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        result = builder.compile().invoke({
            "current_file_transaction": transaction,
            "atomic_implementation_research": [AIMessage(content="", tool_calls=[{
                "name": "get_raw_file_content", "args": args,
                "id": "raw-1", "type": "tool_call",
            }])],
        })
        message = result["atomic_implementation_research"][-1]
        self.assertEqual(message.tool_call_id, "raw-1")
        return json.loads(message.content)


    def test_target_raw_reads_staged_working_without_writing_disk(self):
        target = self.root / "app.py"
        target.write_bytes(b"value = 1\n")
        editor = WorkspaceEditor(self.root)
        transaction = editor.begin("app.py").transaction
        staged = editor.stage(transaction, EditProposal(
            task_id="task-1.step-1", path="app.py",
            operation=EditOperation.EDIT, old_text="value = 1", new_text="value = 2",
            base_hash=transaction.base_hash,
        ))
        self.assertTrue(staged.ok, staged)
        page = self.raw_in_developer(staged.transaction, file_path="app.py")
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["content"], "value = 2\n")
        self.assertEqual(page["view"], "working")
        self.assertEqual(target.read_bytes(), b"value = 1\n")


    def stage_text(self, editor, transaction, text, task_id="stage-1"):
        result = editor.stage(transaction, EditProposal(
            task_id=task_id, path=transaction.path,
            operation=EditOperation.EDIT if transaction.existed or transaction.task_ids else EditOperation.CREATE,
            old_text=transaction.working_content, new_text=text,
            base_hash=transaction.base_hash,
        ))
        self.assertTrue(result.ok, result)
        return result.transaction


    def test_raw_latest_stage_aliases_other_file_and_direct_disk(self):
        target = self.root / "app.py"
        target.write_bytes(b"disk\n")
        (self.root / "other.py").write_bytes(b"other\n")
        editor = WorkspaceEditor(self.root)
        first = self.stage_text(editor, editor.begin("app.py").transaction, "one\n")
        second = self.stage_text(editor, first, "two\n", "stage-2")
        aliases = ["app.py", "./app.py", "workspace_repo/app.py", str(target)]
        if os.name == "nt":
            aliases += ["APP.PY", str(target).upper()]
        for alias in aliases:
            page = self.raw_in_developer(second, file_path=alias)
            self.assertEqual(page["content"], "two\n")
            self.assertEqual(page["view"], "working")
            self.assertEqual(page["path"], "app.py")
        other = self.raw_in_developer(second, file_path="other.py")
        direct = get_raw_file_content.invoke({"file_path": "app.py"})
        for page, content in [(other, "other\n"), (direct, "disk\n")]:
            self.assertEqual(page["view"], "disk")
            self.assertEqual(page["content"], content)
            self.assertNotIn("source_hash", page)
        self.assertEqual(target.read_bytes(), b"disk\n")
        # Model-supplied extra arguments cannot replace ToolNode's real state.
        page = self.raw_in_developer(second, file_path="app.py", state={
            "current_file_transaction": {"working_content": "forged"},
        })
        self.assertEqual(page["content"], "two\n")


    def test_raw_create_and_empty_working_are_readable_before_commit(self):
        editor = WorkspaceEditor(self.root)
        empty = editor.begin("new.py").transaction
        page = self.raw_in_developer(empty, file_path="new.py")
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["content"], "")
        self.assertEqual(page["source_hash"], hashlib.sha256(b"").hexdigest())
        created = self.stage_text(editor, empty, "created\n")
        page = self.raw_in_developer(created, file_path="new.py")
        self.assertEqual(page["content"], "created\n")
        self.assertEqual(page["view"], "working")
        self.assertFalse((self.root / "new.py").exists())
        self.assertFalse(get_raw_file_content.invoke({"file_path": "new.py"})["ok"])


    def test_nested_create_reads_working_without_creating_parent_directories(self):
        editor = WorkspaceEditor(self.root)
        transaction = self.stage_text(editor, editor.begin("new/sub/file.py").transaction, "created\n")
        page = self.raw_in_developer(transaction, file_path="new/sub/file.py")
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["content"], "created\n")
        self.assertFalse((self.root / "new").exists())


    def test_working_raw_pagination_hashes_ranges_and_stale_cursors(self):
        target = self.root / "app.py"
        target.write_bytes(b"d" * (MAX_RAW_PAGE_BYTES + 7))
        editor = WorkspaceEditor(self.root)
        first = self.stage_text(editor, editor.begin("app.py").transaction, "中" * MAX_RAW_PAGE_BYTES)
        page = self.raw_in_developer(first, file_path="app.py")
        cursor = page["continuation"]
        self.assertIsNotNone(cursor)
        complete = page["content"]
        source_hash = hashlib.sha256(first.working_content.encode("utf-8")).hexdigest()
        while True:
            self.assertEqual(page["source_hash"], source_hash)
            self.assertEqual(page["content_hash"], hashlib.sha256(page["content"].encode("utf-8")).hexdigest())
            self.assertLessEqual(len(page["content"].encode("utf-8")), MAX_RAW_PAGE_BYTES)
            if page["continuation"] is None:
                break
            page = self.raw_in_developer(first, file_path="app.py", cursor=page["continuation"])
            self.assertTrue(page["ok"], page)
            complete += page["content"]
        self.assertEqual(complete, first.working_content)
        self.assertNotEqual(page["content_hash"], source_hash)
        second = self.stage_text(editor, first, "新" * MAX_RAW_PAGE_BYTES, "stage-2")
        returned = self.stage_text(editor, second, first.working_content, "stage-3")
        disk = get_raw_file_content.invoke({"file_path": "app.py"})
        for transaction, args in [
            (second, {"file_path": "app.py", "cursor": cursor}),
            (returned, {"file_path": "app.py", "cursor": cursor}),
            (None, {"file_path": "app.py", "cursor": cursor}),
            (first, {"file_path": "app.py", "cursor": disk["continuation"]}),
            (first, {"file_path": "app.py", "cursor": cursor, "start_byte": 3}),
        ]:
            rejected = self.raw_in_developer(transaction, **args)
            self.assertFalse(rejected["ok"], rejected)
            self.assertEqual(rejected["error_code"], "stale_cursor")
        selected = self.raw_in_developer(first, file_path="app.py", start_byte=3, end_byte=9)
        self.assertEqual(selected["content"], "中中")
        self.assertEqual(selected["range"]["start_byte"], 3)
        for args in [{"start_byte": 1}, {"end_byte": 1}, {"end_byte": -1}, {"end_byte": 999999}]:
            self.assertFalse(self.raw_in_developer(first, file_path="app.py", **args)["ok"])


    def test_working_raw_cannot_bypass_policy_parent_or_link_checks(self):
        editor = WorkspaceEditor(self.root)
        transaction = self.stage_text(editor, editor.begin("new.py").transaction, "staged secret")
        policy = WorkspaceAccessPolicy(oracle_paths=("new.py", "hidden"))
        with workspace_root_scope(self.root, access_policy=policy):
            page = self.raw_in_developer(transaction, file_path="new.py")
            self.assertFalse(page["ok"])
            self.assertEqual(page["error_code"], "read_denied")
            self.assertNotIn("staged secret", json.dumps(page))
            self.assertFalse(editor.begin("../escape.py").ok)
            self.assertFalse(self.raw_in_developer(transaction, file_path="../escape.py")["ok"])
            self.assertFalse(self.raw_in_developer(transaction, file_path="hidden/new.py")["ok"])
        # A former legal CREATE gains a non-directory parent before the read.
        (self.root / "parent").mkdir()
        child = editor.begin("parent/new.py").transaction
        (self.root / "parent").rmdir()
        (self.root / "parent").write_bytes(b"not a directory")
        self.assertFalse(self.raw_in_developer(child, file_path="parent/new.py")["ok"])
        # A real owned hardlink to data outside the root must not be read,
        # even when an earlier legitimate CREATE transaction is still active.
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "data.txt"
            external.write_bytes(b"external data")
            os.link(external, self.root / "new.py")
            page = self.raw_in_developer(transaction, file_path="new.py")
            self.assertFalse(page["ok"], page)
            self.assertEqual(page["error_code"], "read_denied")
            (self.root / "new.py").unlink()
            self.assertEqual(external.read_bytes(), b"external data")


    def test_raw_tool_schema_keeps_injected_state_hidden(self):
        schema = get_raw_file_content.tool_call_schema.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"file_path", "start_byte", "end_byte", "cursor"})
        self.assertEqual(schema["required"], ["file_path"])
        self.assertEqual(get_raw_file_content.name, "get_raw_file_content")


    def test_architect_raw_keeps_disk_view(self):
        (self.root / "app.py").write_bytes(b"disk\n")
        builder = StateGraph(SoftwareArchitectState)
        builder.add_node("tools", ToolNode(
            [get_raw_file_content], messages_key="implementation_research_scratchpad"
        ))
        builder.add_edge(START, "tools")
        builder.add_edge("tools", END)
        result = builder.compile().invoke({"implementation_research_scratchpad": [
            AIMessage(content="", tool_calls=[{
                "name": "get_raw_file_content", "args": {"file_path": "app.py"},
                "id": "architect-raw", "type": "tool_call",
            }])
        ]})
        page = json.loads(result["implementation_research_scratchpad"][-1].content)
        self.assertEqual(page["view"], "disk")
        self.assertEqual(page["content"], "disk\n")


    def test_active_create_raw_rejects_directory_link_escape(self):
        editor = WorkspaceEditor(self.root)
        transaction = editor.begin("linked/new.py").transaction
        self.assertIsNotNone(transaction)
        linked = self.root / "linked"
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory).resolve()
            if os.name == "nt":
                # Owned data-only junction; no ACL, candidate execution or
                # AppContainer. Same public mechanism as workspace consumers.
                result = subprocess.run([
                    "pwsh.exe", "-NoLogo", "-NoProfile", "-CommandWithArgs",
                    "New-Item -ItemType Junction -Path $args[0] -Target $args[1] | Out-Null",
                    str(linked), str(outside),
                ], capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(linked.is_junction())
            else:
                linked.symlink_to(outside, target_is_directory=True)
            try:
                page = self.raw_in_developer(transaction, file_path="linked/new.py")
                self.assertFalse(page["ok"], page)
                self.assertEqual(page["error_code"], "path_invalid")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if os.name == "nt":
                    linked.rmdir()
                else:
                    linked.unlink()


    def test_raw_cursor_rejects_offset_before_requested_start(self) -> None:
        (self.root / "range.txt").write_bytes(b"0123456789")
        first = get_raw_file_content.invoke(
            {"file_path": "range.txt", "start_byte": 2, "end_byte": 8}
        )
        self.assertTrue(first["ok"], first)
        cursor = encode_cursor("raw_file", {
            "path": first["path"], "view": first["view"], "start_byte": 2, "end_byte": 8,
            "next_offset": 0, "file_version": first["content_version"],
        })
        result = get_raw_file_content.invoke({
            "file_path": "range.txt", "start_byte": 2, "end_byte": 8, "cursor": cursor,
        })
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error_code"], "stale_cursor")
        self.assertTrue(result["stale"])
        self.assertNotIn("content", result)

    def test_search_cursor_rejects_new_match_in_previously_empty_directory(self) -> None:
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    root = Path(directory)
                    (root / "a").mkdir()
                    (root / "b").mkdir()
                    if unsupported:
                        (root / "a" / "note.bin").write_bytes(b"ignored")
                    for index in range(101):
                        (root / "b" / f"f{index:03}.py").write_text("needle\n", encoding="utf-8")
                    request = {"directory": ".", "search_term": "needle", "context": 0}
                    with workspace_root_scope(root):
                        first = search_keyword_in_directory.invoke(request)
                        self.assertTrue(first["ok"], first)
                        self.assertEqual(len(first["results"]), 100)
                        self.assertIsNotNone(first["continuation"])
                        stable = search_keyword_in_directory.invoke({**request, "cursor": first["continuation"]})
                        self.assertTrue(stable["ok"], stable)
                        self.assertEqual([item["path"] for item in stable["results"]], ["b/f100.py"])
                        (root / "a" / "new.py").write_text("needle\n", encoding="utf-8")
                        stale = search_keyword_in_directory.invoke({**request, "cursor": first["continuation"]})
                        self.assertFalse(stale["ok"], stale)
                        self.assertEqual(stale["error_code"], "stale_cursor")
                        self.assertTrue(stale["stale"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(
            os.environ, {"SWE_AGENT_WORKSPACE": str(self.root)}, clear=False
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_search_covers_utf8_source_config_and_unicode(self) -> None:
        (self.root / "component.js").write_text(
            "const 标记 = 'needle';\n", encoding="utf-8"
        )
        (self.root / "types.ts").write_text(
            "export const needle = '类型';\n", encoding="utf-8"
        )
        (self.root / "settings.toml").write_text(
            "message = 'needle 配置'\n", encoding="utf-8"
        )

        result = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )

        self.assertTrue(result["ok"], result)
        found = {item["path"] for item in result["results"]}
        self.assertEqual(found, {"component.js", "types.ts", "settings.toml"})
        self.assertTrue(all(item["content_hash"] for item in result["results"]))

    def test_search_short_query_empty_result_and_invalid_files_are_explicit(self) -> None:
        (self.root / "plain.py").write_text("nothing here\n", encoding="utf-8")
        (self.root / "invalid.json").write_bytes(b"{\xff}")
        (self.root / "binary.toml").write_bytes(b"key=one\x00two")

        short = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "ab"}
        )
        empty = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )

        self.assertFalse(short["ok"])
        self.assertEqual(short["error_code"], "invalid_request")
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["results"], [])
        self.assertEqual(empty["skipped_files"], ["binary.toml", "invalid.json"])
        self.assertTrue(empty["warnings"])
        self.assertGreaterEqual(empty["scanned_bytes"], len(b"nothing here\n"))
        self.assertLessEqual(
            empty["scanned_bytes"], empty["limits"]["max_bytes"]
        )

    def test_search_ignores_dependency_and_build_directories(self) -> None:
        for name in (".git", "node_modules", "build", ".venv", "__pycache__"):
            nested = self.root / name
            nested.mkdir()
            (nested / "hidden.py").write_text("needle\n", encoding="utf-8")
        (self.root / "visible.py").write_text("needle\n", encoding="utf-8")

        result = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual({item["path"] for item in result["results"]}, {"visible.py"})
        self.assertNotIn("hidden.py", result["content"])

    def test_search_enforces_file_result_and_output_byte_caps(self) -> None:
        limits = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )["limits"]
        for index in range(limits["max_files"] + 1):
            (self.root / f"f{index:04}.js").write_text(
                "needle\n", encoding="utf-8"
            )
        by_files = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )
        self.assertLessEqual(by_files["files_considered"], limits["max_files"])
        self.assertTrue(by_files["truncated"])

        for path in self.root.iterdir():
            path.unlink()
        (self.root / "long.js").write_text(
            "needle " + "x" * (limits["max_bytes"] * 2) + "\nneedle later\n",
            encoding="utf-8",
        )
        first_page = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self._assert_search_page_is_bounded(first_page)
        self.assertTrue(first_page["truncated"])
        self.assertIsNotNone(first_page["continuation"])

        pages = [first_page]
        cursor = first_page["continuation"]
        while cursor is not None:
            page = search_keyword_in_directory.invoke(
                {
                    "directory": ".",
                    "search_term": "needle",
                    "context": 0,
                    "cursor": cursor,
                }
            )
            self._assert_search_page_is_bounded(page)
            pages.append(page)
            cursor = page["continuation"]
            self.assertLessEqual(len(pages), 4)
        self.assertEqual(
            [item["match_line"] for page in pages for item in page["results"]],
            [1, 2],
        )

    def test_search_result_cap_and_long_line_are_bounded(self) -> None:
        limits = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle"}
        )["limits"]
        for index in range(limits["max_results"] + 5):
            (self.root / f"match-{index:03}.ts").write_text(
                "needle\n", encoding="utf-8"
            )
        many = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self.assertEqual(len(many["results"]), limits["max_results"])
        self.assertTrue(many["truncated"])
        self.assertLessEqual(
            many["evidence_bytes"], limits["max_bytes"]
        )

        for path in self.root.iterdir():
            path.unlink()
        (self.root / "long-line.ts").write_text(
            "needle" + "雪" * (limits["max_bytes"] // 3 - 80), encoding="utf-8"
        )
        long_line = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self.assertTrue(long_line["truncated"])
        self.assertIn("result_snippet_truncated", long_line["warnings"])
        self.assertLessEqual(
            long_line["evidence_bytes"], limits["max_bytes"]
        )

    def test_search_reports_scan_errors_without_silent_success(self) -> None:
        (self.root / "file.py").write_text("needle\n", encoding="utf-8")
        with patch("agent.tools.search.os.scandir", side_effect=PermissionError("denied")):
            result = search_keyword_in_directory.invoke(
                {"directory": ".", "search_term": "needle"}
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "scan_failed")

    def test_search_bounds_public_directory_enumeration_and_fails_closed(self) -> None:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                return False

            def is_symlink(self) -> bool:
                return False

        class GuardedScandir:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= MAX_SEARCH_SCAN_ENTRIES + 1:
                    raise AssertionError("search enumerated beyond its hard bound")
                index = self.consumed
                self.consumed += 1
                if index == MAX_SEARCH_SCAN_ENTRIES:
                    return Entry("late-supported-match.py")
                return Entry(f"unsupported-{index:05}.bin")

        scanner = GuardedScandir()
        with patch("agent.tools.search.os.scandir", return_value=scanner):
            result = search_keyword_in_directory.invoke(
                {"directory": ".", "search_term": "needle"}
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(scanner.consumed, MAX_SEARCH_SCAN_ENTRIES + 1)
        self.assertTrue(result["truncated"])
        self.assertIn("directory_scan_entry_limit_reached", result["warnings"])
        self.assertIsNone(result["continuation"])
        self.assertNotIn("late-supported-match.py", result["content"])
        self.assertEqual(result["results"], [])
        self.assertEqual(
            result["limits"]["max_scanned_entries"], MAX_SEARCH_SCAN_ENTRIES
        )

    def test_search_exact_scan_cap_with_eof_is_complete(self) -> None:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                return False

            def is_symlink(self) -> bool:
                return False

        class ExactCapScandir:
            def __init__(self) -> None:
                self.entries = [
                    Entry(f"unsupported-{index:05}.bin")
                    for index in range(MAX_SEARCH_SCAN_ENTRIES)
                ]
                self.consumed = 0
                self.next_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                self.next_calls += 1
                if self.consumed == len(self.entries):
                    raise StopIteration
                entry = self.entries[self.consumed]
                self.consumed += 1
                return entry

        scanner = ExactCapScandir()
        with patch("agent.tools.search.os.scandir", return_value=scanner):
            result = search_keyword_in_directory.invoke(
                {"directory": ".", "search_term": "needle"}
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(scanner.consumed, MAX_SEARCH_SCAN_ENTRIES)
        self.assertEqual(scanner.next_calls, MAX_SEARCH_SCAN_ENTRIES + 1)
        self.assertFalse(result["truncated"])
        self.assertNotIn("directory_scan_entry_limit_reached", result["warnings"])
        self.assertIsNone(result["continuation"])

    def test_search_exact_scan_cap_checks_pending_directories(self) -> None:
        class Entry:
            def __init__(
                self, name: str, *, is_directory: bool = False
            ) -> None:
                self.name = name
                self._is_directory = is_directory

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                return self._is_directory

            def is_symlink(self) -> bool:
                return False

        class Scandir:
            def __init__(self, entries) -> None:
                self.entries = list(entries)
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= len(self.entries):
                    raise StopIteration
                entry = self.entries[self.consumed]
                self.consumed += 1
                return entry

        def run_case(child_name: str, child_entries):
            child_path = self.root / child_name
            child_path.mkdir()
            parent_scanner = Scandir(
                [
                    Entry(f"unsupported-{index:05}.bin")
                    for index in range(MAX_SEARCH_SCAN_ENTRIES - 1)
                ]
                + [Entry(child_name, is_directory=True)]
            )
            child_scanner = Scandir(child_entries)

            def scanner_for(path):
                candidate = Path(path)
                if candidate == self.root:
                    return parent_scanner
                if candidate == child_path:
                    return child_scanner
                raise AssertionError(f"unexpected directory scan: {candidate}")

            with patch("agent.tools.search.os.scandir", side_effect=scanner_for):
                result = search_keyword_in_directory.invoke(
                    {"directory": ".", "search_term": "needle"}
                )
            self.assertEqual(parent_scanner.consumed, MAX_SEARCH_SCAN_ENTRIES)
            return result, child_scanner

        empty_result, empty_scanner = run_case("empty-pending", [])
        self.assertTrue(empty_result["ok"], empty_result)
        self.assertFalse(empty_result["truncated"])
        self.assertNotIn(
            "directory_scan_entry_limit_reached", empty_result["warnings"]
        )
        self.assertEqual(empty_scanner.consumed, 0)

        incomplete_result, nonempty_scanner = run_case(
            "nonempty-pending", [Entry("later.py")]
        )
        self.assertTrue(incomplete_result["ok"], incomplete_result)
        self.assertTrue(incomplete_result["truncated"])
        self.assertIn(
            "directory_scan_entry_limit_reached", incomplete_result["warnings"]
        )
        self.assertEqual(nonempty_scanner.consumed, 1)
        self.assertIsNone(incomplete_result["continuation"])

    def test_search_filtered_entries_consume_scan_budget_without_leaking_names(
        self,
    ) -> None:
        class Entry:
            def __init__(self, name: str, *, is_directory: bool = False) -> None:
                self.name = name
                self._is_directory = is_directory

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                if self.name in {".git", "oracle"}:
                    raise AssertionError("protected path metadata was probed")
                return self._is_directory

            def is_symlink(self) -> bool:
                return False

        class GuardedScandir:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= MAX_SEARCH_SCAN_ENTRIES + 1:
                    raise AssertionError("search enumerated beyond its hard bound")
                index = self.consumed
                self.consumed += 1
                entries = (
                    Entry(".git", is_directory=True),
                    Entry("oracle", is_directory=True),
                    Entry("node_modules", is_directory=True),
                    Entry("unsafe-link.py"),
                    Entry("unsupported.bin"),
                    Entry(".env.private-canary"),
                )
                if index == MAX_SEARCH_SCAN_ENTRIES:
                    return Entry("late-supported-match.py")
                return entries[index % len(entries)]

        scanner = GuardedScandir()
        policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
        with (
            workspace_root_scope(self.root, access_policy=policy),
            patch("agent.tools.search.os.scandir", return_value=scanner),
            patch(
                "agent.tools.search._is_link_or_junction",
                side_effect=lambda path: path.name == "unsafe-link.py",
            ),
        ):
            result = search_keyword_in_directory.invoke(
                {"directory": ".", "search_term": "needle"}
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(scanner.consumed, MAX_SEARCH_SCAN_ENTRIES + 1)
        self.assertTrue(result["truncated"])
        self.assertIn("directory_scan_entry_limit_reached", result["warnings"])
        self.assertIsNone(result["continuation"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["content"], "No matches found.")
        serialized = json.dumps(result, ensure_ascii=False)
        for protected_name in (".git", "oracle", ".env.private-canary"):
            self.assertNotIn(protected_name, serialized)
        self.assertNotIn("late-supported-match.py", serialized)

    def test_search_preserves_order_for_a_complete_small_workspace(self) -> None:
        (self.root / "b.py").write_text("needle\n", encoding="utf-8")
        (self.root / "a.py").write_text("needle\n", encoding="utf-8")
        for directory, name in (("z", "d.py"), ("a", "c.py")):
            nested = self.root / directory
            nested.mkdir()
            (nested / name).write_text("needle\n", encoding="utf-8")

        result = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["continuation"])
        self.assertEqual(
            [item["path"] for item in result["results"]],
            ["a.py", "b.py", "a/c.py", "z/d.py"],
        )

    def test_search_result_cap_continuation_has_no_duplicates_or_gaps(self) -> None:
        expected = set()
        for index in range(MAX_SEARCH_RESULTS + 5):
            name = f"match-{index:03}.ts"
            expected.add(name)
            (self.root / name).write_text("needle\n", encoding="utf-8")

        pages = []
        cursor = None
        while True:
            page = search_keyword_in_directory.invoke(
                {
                    "directory": ".",
                    "search_term": "needle",
                    "context": 0,
                    "cursor": cursor,
                }
            )
            self._assert_search_page_is_bounded(page)
            pages.append(page)
            cursor = page["continuation"]
            if cursor is None:
                break
            self.assertTrue(page["truncated"])
            self.assertLessEqual(len(pages), 3)

        found = [item["path"] for page in pages for item in page["results"]]
        self.assertEqual(set(found), expected)
        self.assertEqual(len(found), len(expected))
        self.assertFalse(pages[-1]["truncated"])
        self.assertIsNone(pages[-1]["continuation"])

    def test_search_file_cap_continuation_reaches_later_files(self) -> None:
        for index in range(MAX_SEARCH_FILES):
            (self.root / f"a-{index:03}.py").write_text(
                "no matching term\n", encoding="utf-8"
            )
        expected = {"z-after-limit-1.py", "z-after-limit-2.py"}
        for name in expected:
            (self.root / name).write_text("needle\n", encoding="utf-8")

        first = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self._assert_search_page_is_bounded(first)
        self.assertEqual(first["files_considered"], MAX_SEARCH_FILES)
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["continuation"])

        second = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "needle",
                "context": 0,
                "cursor": first["continuation"],
            }
        )
        self._assert_search_page_is_bounded(second)
        self.assertEqual({item["path"] for item in second["results"]}, expected)
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["continuation"])

    def test_search_byte_and_long_line_caps_continue_with_later_evidence(self) -> None:
        suffix = "\nneedle later\n"
        target_size = MAX_SEARCH_BYTES - 64
        first_line_size = target_size - len(suffix.encode("utf-8")) - 1
        content = "needle" + "x" * (first_line_size - len("needle")) + suffix
        self.assertLessEqual(len(content.encode("utf-8")), MAX_SEARCH_BYTES)
        (self.root / "long.py").write_text(content, encoding="utf-8")

        first = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self._assert_search_page_is_bounded(first)
        self.assertEqual(len(first["results"]), 1)
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["continuation"])

        second = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "needle",
                "context": 0,
                "cursor": first["continuation"],
            }
        )
        self._assert_search_page_is_bounded(second)
        self.assertEqual(len(second["results"]), 1)
        self.assertEqual(second["results"][0]["match_line"], 2)
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["continuation"])

    def test_search_scanned_byte_cap_continues_at_deferred_file(self) -> None:
        first_size = MAX_SEARCH_BYTES * 3 // 5
        first_content = "needle\n" + "x" * (first_size - len("needle\n"))
        second_content = "y" * (first_size - len("\nneedle\n")) + "\nneedle\n"
        (self.root / "a-first.py").write_text(first_content, encoding="utf-8")
        (self.root / "b-second.py").write_text(second_content, encoding="utf-8")

        first = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )

        self._assert_search_page_is_bounded(first)
        self.assertEqual([item["path"] for item in first["results"]], ["a-first.py"])
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["continuation"])

        second = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "needle",
                "context": 0,
                "cursor": first["continuation"],
            }
        )

        self._assert_search_page_is_bounded(second)
        self.assertEqual([item["path"] for item in second["results"]], ["b-second.py"])
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["continuation"])

    def test_search_chunk_cursor_preserves_utf8_match_across_byte_boundary(self) -> None:
        target = self.root / "unicode-long-line.js"
        target.write_text(
            "x" * (MAX_SEARCH_BYTES - 1) + "雪needle later\n", encoding="utf-8"
        )

        first = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "雪needle",
                "context": 0,
            }
        )

        self._assert_search_page_is_bounded(first)
        self.assertEqual(first["results"], [])
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["continuation"])

        second = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "雪needle",
                "context": 0,
                "cursor": first["continuation"],
            }
        )

        self._assert_search_page_is_bounded(second)
        self.assertEqual(len(second["results"]), 1)
        self.assertEqual(second["results"][0]["match_line"], 1)
        self.assertEqual(second["results"][0]["content_hash_scope"], "scanned_segment")

    def test_search_cursor_rejects_changed_query_or_context(self) -> None:
        for index in range(MAX_SEARCH_RESULTS + 1):
            (self.root / f"f-{index:03}.ts").write_text(
                "needle other\n", encoding="utf-8"
            )
        first = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self.assertIsNotNone(first["continuation"])
        (self.root / "other").mkdir()

        for directory, query in (
            (".", {"search_term": "other", "context": 0}),
            (".", {"search_term": "needle", "context": 1}),
            ("other", {"search_term": "needle", "context": 0}),
        ):
            stale = search_keyword_in_directory.invoke(
                {
                    "directory": directory,
                    **query,
                    "cursor": first["continuation"],
                }
            )
            self.assertFalse(stale["ok"])
            self.assertEqual(stale["error_code"], "stale_cursor")
            self.assertTrue(stale["stale"])

    def test_search_cursor_rejects_changed_page_evidence(self) -> None:
        for index in range(MAX_SEARCH_RESULTS + 1):
            (self.root / f"f-{index:03}.ts").write_text(
                "needle\n", encoding="utf-8"
            )
        first = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self.assertIsNotNone(first["continuation"])
        (self.root / "f-000.ts").write_text("changed\n", encoding="utf-8")

        stale = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "needle",
                "context": 0,
                "cursor": first["continuation"],
            }
        )

        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error_code"], "stale_cursor")
        self.assertTrue(stale["stale"])

        (self.root / "f-000.ts").write_text("needle\n", encoding="utf-8")
        next_page = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        (self.root / "z-new.ts").write_text("needle\n", encoding="utf-8")
        stale_directory = search_keyword_in_directory.invoke(
            {
                "directory": ".",
                "search_term": "needle",
                "context": 0,
                "cursor": next_page["continuation"],
            }
        )
        self.assertFalse(stale_directory["ok"])
        self.assertEqual(stale_directory["error_code"], "stale_cursor")

    def _assert_search_page_is_bounded(self, page: dict[str, object]) -> None:
        self.assertTrue(page["ok"], page)
        limits = page["limits"]
        self.assertLessEqual(page["files_considered"], limits["max_files"])
        self.assertLessEqual(page["files_scanned"], limits["max_files"])
        self.assertLessEqual(len(page["results"]), limits["max_results"])
        self.assertLessEqual(page["scanned_bytes"], limits["max_bytes"])
        self.assertLessEqual(page["evidence_bytes"], limits["max_bytes"])
        self.assertLessEqual(
            len(page["content"].encode("utf-8")), limits["max_bytes"]
        )
        if page["continuation"] is not None:
            self.assertLessEqual(len(page["continuation"]), MAX_CURSOR_CHARS)

    def test_raw_read_paginates_utf8_with_content_hash_and_bound_cursor(self) -> None:
        content = ("alpha 雪\n" * 20_000).encode("utf-8")
        target = self.root / "large.txt"
        target.write_bytes(content)
        first = get_raw_file_content.invoke({"file_path": "large.txt"})
        self.assertTrue(first["ok"], first)
        page_limit = first["limits"]["max_bytes"]
        self.assertLessEqual(len(first["content"].encode("utf-8")), page_limit)
        self.assertEqual(first["content_hash"], _sha256(first["content"].encode("utf-8")))
        self.assertTrue(first["truncated"])

        pages = [first]
        cursor = first["continuation"]
        while cursor is not None:
            page = get_raw_file_content.invoke(
                {
                    "file_path": "large.txt",
                    "start_byte": 0,
                    "end_byte": None,
                    "cursor": cursor,
                }
            )
            self.assertTrue(page["ok"], page)
            self.assertLessEqual(
                len(page["content"].encode("utf-8")), page_limit
            )
            pages.append(page)
            cursor = page["continuation"]

        self.assertEqual("".join(page["content"] for page in pages).encode("utf-8"), content)
        self.assertEqual(pages[0]["range"]["start_byte"], 0)
        self.assertEqual(pages[-1]["range"]["end_byte"], len(content))

    def test_raw_read_rejects_invalid_ranges_and_stale_or_mismatched_cursor(self) -> None:
        target = self.root / "mutable.txt"
        target.write_text("x" * 200_000, encoding="utf-8")
        invalid = get_raw_file_content.invoke(
            {"file_path": "mutable.txt", "start_byte": -1}
        )
        first = get_raw_file_content.invoke({"file_path": "mutable.txt"})
        target.write_text("y" * 200_000, encoding="utf-8")
        stale = get_raw_file_content.invoke(
            {"file_path": "mutable.txt", "cursor": first["continuation"]}
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error_code"], "invalid_range")
        self.assertFalse(stale["ok"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["error_code"], "stale_cursor")

        new_first = get_raw_file_content.invoke({"file_path": "mutable.txt"})
        changed_query = get_raw_file_content.invoke(
            {
                "file_path": "mutable.txt",
                "start_byte": 1,
                "cursor": new_first["continuation"],
            }
        )
        self.assertEqual(changed_query["error_code"], "stale_cursor")

    def test_raw_read_honors_requested_byte_range_and_rejects_bad_cursor(self) -> None:
        (self.root / "range.txt").write_text("0123456789", encoding="utf-8")
        result = get_raw_file_content.invoke(
            {"file_path": "range.txt", "start_byte": 2, "end_byte": 8}
        )
        bad_cursor = get_raw_file_content.invoke(
            {"file_path": "range.txt", "cursor": "invalid"}
        )
        self.assertEqual(result["content"], "234567")
        self.assertEqual(result["range"], {
            "start_byte": 2,
            "end_byte": 8,
            "total_bytes": 10,
        })
        self.assertIsNone(result["continuation"])
        self.assertEqual(bad_cursor["error_code"], "invalid_cursor")

    def test_tree_has_depth_entry_bounds_and_versioned_continuation(self) -> None:
        limits = get_files_structure.invoke({"directory": "."})["limits"]
        for index in range(limits["max_entries"] + 2):
            (self.root / f"file-{index:04}.py").write_text("x\n", encoding="utf-8")
        nested = self.root / "one" / "two" / "three"
        nested.mkdir(parents=True)
        (nested / "deep.py").write_text("x\n", encoding="utf-8")

        first = get_files_structure.invoke(
            {"directory": ".", "max_depth": 1, "max_entries": 10}
        )
        self.assertTrue(first["ok"], first)
        self.assertLessEqual(first["range"]["entry_count"], 10)
        self.assertNotIn("deep.py", first["content"])
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["continuation"])

        page = get_files_structure.invoke(
            {
                "directory": ".",
                "max_depth": 1,
                "max_entries": 10,
                "cursor": first["continuation"],
            }
        )
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["range"]["start_entry"], 10)

        changed_query = get_files_structure.invoke(
            {
                "directory": ".",
                "max_depth": 2,
                "max_entries": 10,
                "cursor": first["continuation"],
            }
        )
        self.assertFalse(changed_query["ok"])
        self.assertTrue(changed_query["stale"])
        self.assertEqual(changed_query["error_code"], "stale_cursor")

        (self.root / "file-0000.py").write_text("changed\n", encoding="utf-8")
        stale = get_files_structure.invoke(
            {
                "directory": ".",
                "max_depth": 1,
                "max_entries": 10,
                "cursor": first["continuation"],
            }
        )
        self.assertFalse(stale["ok"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["error_code"], "stale_cursor")

    def test_tree_hard_caps_model_requested_depth_and_entry_values(self) -> None:
        limits = get_files_structure.invoke({"directory": "."})["limits"]
        too_many = get_files_structure.invoke(
            {"directory": ".", "max_entries": limits["max_entries"] + 1}
        )
        too_deep = get_files_structure.invoke(
            {"directory": ".", "max_depth": limits["max_depth"] + 1}
        )
        self.assertFalse(too_many["ok"])
        self.assertFalse(too_deep["ok"])
        self.assertEqual(too_many["error_code"], "invalid_request")
        self.assertEqual(too_deep["error_code"], "invalid_request")

    def test_tree_depth_is_relative_to_requested_subdirectory(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "child.py").write_text("x\n", encoding="utf-8")

        result = get_files_structure.invoke(
            {"directory": "nested", "max_depth": 1}
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["range"]["total_entries"], 1)
        self.assertEqual(result["content"].splitlines(), ["nested", "child.py"])

    def test_tree_scan_cap_counts_entries_denied_by_link_policy(self) -> None:
        class Entry:
            def __init__(self, name: str, is_directory: bool) -> None:
                self.name = name
                self._is_directory = is_directory

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                return self._is_directory

            def is_file(self, *, follow_symlinks: bool = True) -> bool:
                return not self._is_directory

            def is_symlink(self) -> bool:
                return False

        class GuardedScandir:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= MAX_TREE_SCAN_ENTRIES + 1:
                    raise AssertionError("tree traversal consumed an unbounded suffix")
                index = self.consumed
                self.consumed += 1
                if index % 3 == 0:
                    return Entry(".git", True)
                if index % 3 == 1:
                    return Entry("oracle", True)
                return Entry(f"linked-{index:05}.py", False)

        scanner = GuardedScandir()
        policy = WorkspaceAccessPolicy(oracle_paths=("oracle",))
        with (
            workspace_root_scope(self.root, access_policy=policy),
            patch("agent.tools.write.os.scandir", return_value=scanner),
            patch(
                "agent.tools.write._is_link_or_junction",
                side_effect=lambda path: path.name.startswith("linked-"),
            ),
        ):
            result = get_files_structure.invoke({"directory": "."})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertIn("tree_scan_entry_limit_reached", result["warnings"])
        self.assertEqual(result["range"]["total_entries"], 0)
        self.assertEqual(scanner.consumed, MAX_TREE_SCAN_ENTRIES + 1)
        self.assertEqual(
            result["limits"]["max_scanned_entries"], MAX_TREE_SCAN_ENTRIES
        )

    def test_tree_public_tool_does_not_materialize_oversized_directory(self) -> None:
        (self.root / "entry.py").write_text("entry\n", encoding="utf-8")

        class Entry:
            name = "entry.py"

            def is_dir(self, *, follow_symlinks: bool = True) -> bool:
                return False

            def is_file(self, *, follow_symlinks: bool = True) -> bool:
                return True

            def is_symlink(self) -> bool:
                return False

        class GuardedScandir:
            def __init__(self) -> None:
                self.consumed = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                if self.consumed >= MAX_TREE_SCAN_ENTRIES + 1:
                    raise AssertionError("tree traversal consumed an unbounded suffix")
                self.consumed += 1
                return Entry()

        scanner = GuardedScandir()
        policy = WorkspaceAccessPolicy(hidden_paths=("entry.py",))
        with (
            workspace_root_scope(self.root, access_policy=policy),
            patch("agent.tools.write.os.scandir", return_value=scanner),
        ):
            result = get_files_structure.invoke({"directory": "."})

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["truncated"])
        self.assertIn("tree_scan_entry_limit_reached", result["warnings"])
        self.assertLessEqual(
            result["range"]["entry_count"], result["limits"]["max_entries"]
        )
        self.assertEqual(scanner.consumed, MAX_TREE_SCAN_ENTRIES + 1)


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
