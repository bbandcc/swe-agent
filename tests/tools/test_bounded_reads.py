import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.codemap import get_raw_file_content
from agent.tools.search import search_keyword_in_directory
from agent.tools.write import get_files_structure


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
            ("needle " + "x" * (limits["max_bytes"] * 3) + "\n") * 8,
            encoding="utf-8",
        )
        by_results = search_keyword_in_directory.invoke(
            {"directory": ".", "search_term": "needle", "context": 0}
        )
        self.assertLessEqual(len(by_results["results"]), limits["max_results"])
        self.assertLessEqual(
            len(by_results["content"].encode("utf-8")), limits["max_bytes"]
        )
        self.assertTrue(by_results["truncated"])
        self.assertLessEqual(
            len(by_results["content"].encode("utf-8")), limits["max_bytes"]
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
        limits = get_files_structure.invoke({"directory": "."})["limits"]
        names = [
            f"linked-{index:04}.py"
            for index in range(limits["max_scanned_entries"] + 1)
        ]
        with (
            patch(
                "agent.tools.write.os.walk",
                return_value=[(str(self.root), [], names)],
            ),
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


def _sha256(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
