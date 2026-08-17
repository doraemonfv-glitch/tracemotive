from __future__ import annotations

import unittest

from tracemotive.structured_diff import (
    MAX_STRUCTURED_DIFF_DEPTH,
    MAX_STRUCTURED_DIFF_NODES,
    MAX_STRUCTURED_DIFF_RECORDS,
    structured_diff,
)


class StructuredDiffTests(unittest.TestCase):
    def test_sorted_object_operations_pointer_escaping_and_null_absence(self) -> None:
        left = {
            "z": 1,
            "a": {"a/b": None, "~key": 1},
            "same": {"unicode": "東京", "control": "line\nend"},
        }
        right = {
            "a": {"a/b": "changed", "~key": True, "added": "<script>"},
            "same": {"unicode": "東京", "control": "line\nend"},
        }

        result = structured_diff(left, right, path_prefix="/input")

        self.assertFalse(result.truncated)
        self.assertEqual(
            [(item["op"], item["path"]) for item in result.records],
            [
                ("replace", "/input/a/a~1b"),
                ("add", "/input/a/added"),
                ("replace", "/input/a/~0key"),
                ("remove", "/input/z"),
            ],
        )
        self.assertEqual(result.records[0]["left"], {"state": "present", "value": None})
        self.assertEqual(result.records[0]["right"], {"state": "present", "value": "changed"})
        self.assertEqual(result.records[1]["left"], {"state": "absent", "value": None})
        self.assertEqual(result.records[1]["right"], {"state": "present", "value": "<script>"})
        self.assertEqual(result.records[2]["left"]["value"], 1)
        self.assertIs(result.records[2]["right"]["value"], True)

    def test_object_key_reorder_and_exact_primitive_types_are_honest(self) -> None:
        self.assertEqual(structured_diff({"a": 1, "b": 2}, {"b": 2, "a": 1}).records, ())
        result = structured_diff(
            {"integer": 1, "boolean": True, "text": "1"},
            {"integer": 1.0, "boolean": 1, "text": 1},
        )
        self.assertEqual(
            [item["path"] for item in result.records],
            ["/boolean", "/integer", "/text"],
        )

    def test_scalar_arrays_use_positions_without_move_or_identity_claims(self) -> None:
        result = structured_diff(
            {"items": ["same", "repeat", "repeat"]},
            {"items": ["repeat", "same", "repeat", "new"]},
        )

        self.assertEqual(
            [(item["op"], item["path"]) for item in result.records],
            [("replace", "/items/0"), ("replace", "/items/1"), ("add", "/items/3")],
        )
        self.assertTrue(all("move" not in item for item in result.records))
        self.assertTrue(all(item["reason"] is None for item in result.records))

    def test_complex_arrays_fall_back_to_one_whole_array_record(self) -> None:
        result = structured_diff(
            {"items": [{"id": 1}, {"id": 2}]},
            {"items": [{"id": 2}, {"id": 1}]},
        )

        self.assertFalse(result.truncated)
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0]["op"], "replace")
        self.assertEqual(result.records[0]["path"], "/items")
        self.assertIsNone(result.records[0]["reason"])

    def test_bounds_are_explicit_and_return_records_collected_before_the_bound(self) -> None:
        deep_left: dict[str, object] = {}
        deep_right: dict[str, object] = {}
        left_cursor = deep_left
        right_cursor = deep_right
        for index in range(MAX_STRUCTURED_DIFF_DEPTH + 2):
            left_cursor["nested"] = {}
            right_cursor["nested"] = {}
            left_cursor = left_cursor["nested"]  # type: ignore[assignment]
            right_cursor = right_cursor["nested"]  # type: ignore[assignment]
        left_cursor["value"] = "left"
        right_cursor["value"] = "right"
        deep = structured_diff(deep_left, deep_right)
        self.assertTrue(deep.truncated)
        self.assertEqual(deep.reason, "max_depth")

        nodes = structured_diff({str(index): index for index in range(8)}, {}, max_nodes=3)
        self.assertTrue(nodes.truncated)
        self.assertEqual(nodes.reason, "max_nodes")

        records = structured_diff({str(index): index for index in range(8)}, {}, max_records=2)
        self.assertTrue(records.truncated)
        self.assertEqual(records.reason, "max_change_records")
        self.assertEqual(len(records.records), 2)

        self.assertEqual(MAX_STRUCTURED_DIFF_NODES, 4096)
        self.assertEqual(MAX_STRUCTURED_DIFF_RECORDS, 256)

    def test_depth_bound_keeps_later_in_bound_sibling_changes(self) -> None:
        # Keys are compared in reverse sorted order, so "a" is expanded first.
        # A too-deep subtree must not prevent later in-bound siblings from
        # emitting the records collected before the bound.
        result = structured_diff(
            {"a": {"b": {"c": 1}}, "d": "left"},
            {"a": {"b": {"c": 2}}, "d": "right"},
            max_depth=2,
        )

        self.assertTrue(result.truncated)
        self.assertEqual(result.reason, "max_depth")
        self.assertEqual(
            [(item["op"], item["path"], item["left"]["value"], item["right"]["value"]) for item in result.records],
            [("replace", "/d", "left", "right")],
        )

    def test_node_and_output_bounds_stop_the_remaining_walk(self) -> None:
        # max_nodes is a global visited-node budget. After it is exhausted,
        # later unvisited siblings are omitted from the projection.
        nodes = structured_diff(
            {"z": {str(index): index for index in range(10)}, "a": "left"},
            {"z": {str(index): index + 1 for index in range(10)}, "a": "right"},
            max_nodes=3,
        )
        self.assertTrue(nodes.truncated)
        self.assertEqual(nodes.reason, "max_nodes")
        self.assertEqual([item["path"] for item in nodes.records], ["/a"])

        # max_records is a global output budget. Once the cap is reached, no
        # further change records are emitted.
        records = structured_diff(
            {"z": 1, "m": 2, "a": 3},
            {"z": 9, "m": 8, "a": 7},
            max_records=1,
        )
        self.assertTrue(records.truncated)
        self.assertEqual(records.reason, "max_change_records")
        self.assertEqual([item["path"] for item in records.records], ["/a"])

        # Keys are compared in reverse sorted order, so "z" is visited first.
        # After an oversized record is omitted, a later otherwise-valid small
        # sibling must not be emitted.
        oversized = structured_diff(
            {"z": "x" * 200, "a": "left"},
            {"z": "y" * 200, "a": "right"},
            max_value_bytes=80,
        )
        self.assertTrue(oversized.truncated)
        self.assertEqual(oversized.reason, "max_value_bytes")
        self.assertEqual(oversized.records, ())

    def test_large_values_have_an_explicit_value_bound(self) -> None:
        result = structured_diff({"blob": "x"}, {"blob": "x" * 1000}, max_value_bytes=128)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reason, "max_value_bytes")
        self.assertEqual(result.records, ())

    def test_invalid_pointer_prefix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            structured_diff({}, {}, path_prefix="not-a-pointer")


if __name__ == "__main__":
    unittest.main()
