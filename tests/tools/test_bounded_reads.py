import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.codemap import get_raw_file_content
from agent.tools.read_contract import (
    MAX_CURSOR_CHARS,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_RESULTS,
    MAX_TREE_SCAN_ENTRIES,
)
from agent.tools.search import search_keyword_in_directory
from agent.tools.write import get_files_structure
from agent.workspace import WorkspaceAccessPolicy, workspace_root_scope


class BoundedReadToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
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
        with patch("agent.tools.search.os.walk", side_effect=PermissionError("denied")):
            result = search_keyword_in_directory.invoke(
                {"directory": ".", "search_term": "needle"}
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "scan_failed")

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
