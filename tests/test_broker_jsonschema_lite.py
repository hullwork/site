from __future__ import annotations

import unittest

from sites.broker import jsonschema_lite as js


class TypeTests(unittest.TestCase):
    def test_string_type_ok(self) -> None:
        self.assertEqual(js.validate("hi", {"type": "string"}), [])

    def test_string_type_rejects_int(self) -> None:
        errors = js.validate(3, {"type": "string"})
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].path, "$")

    def test_boolean_is_not_integer_or_number(self) -> None:
        self.assertTrue(js.validate(True, {"type": "integer"}))
        self.assertTrue(js.validate(True, {"type": "number"}))
        self.assertEqual(js.validate(True, {"type": "boolean"}), [])

    def test_integer_is_also_number(self) -> None:
        self.assertEqual(js.validate(5, {"type": "number"}), [])

    def test_float_is_not_integer(self) -> None:
        self.assertTrue(js.validate(5.5, {"type": "integer"}))

    def test_null_type(self) -> None:
        self.assertEqual(js.validate(None, {"type": "null"}), [])
        self.assertTrue(js.validate(0, {"type": "null"}))

    def test_union_type(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(js.validate("x", schema), [])
        self.assertEqual(js.validate(None, schema), [])
        self.assertTrue(js.validate(5, schema))


class ObjectTests(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "age": {"type": "integer"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }

    def test_valid_object(self) -> None:
        self.assertEqual(js.validate({"name": "a", "age": 3}, self.SCHEMA), [])

    def test_missing_required_field(self) -> None:
        errors = js.validate({"age": 3}, self.SCHEMA)
        self.assertEqual([e.path for e in errors], ["$.name"])
        self.assertIn("required", errors[0].message)

    def test_additional_property_rejected(self) -> None:
        errors = js.validate({"name": "a", "extra": 1}, self.SCHEMA)
        self.assertEqual([e.path for e in errors], ["$.extra"])

    def test_nested_property_error_path(self) -> None:
        errors = js.validate({"name": 5}, self.SCHEMA)
        self.assertEqual([e.path for e in errors], ["$.name"])

    def test_additional_properties_true_allows_extras(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.assertEqual(js.validate({"a": "x", "b": 1}, schema), [])


class ArrayTests(unittest.TestCase):
    def test_items_validated_with_index_path(self) -> None:
        schema = {"type": "array", "items": {"type": "integer"}}
        errors = js.validate([1, "two", 3], schema)
        self.assertEqual([e.path for e in errors], ["$[1]"])

    def test_min_max_items(self) -> None:
        schema = {"type": "array", "minItems": 2, "maxItems": 3}
        self.assertTrue(js.validate([1], schema))
        self.assertTrue(js.validate([1, 2, 3, 4], schema))
        self.assertEqual(js.validate([1, 2], schema), [])


class EnumTests(unittest.TestCase):
    def test_enum_allows_listed_value(self) -> None:
        self.assertEqual(js.validate("b", {"enum": ["a", "b"]}), [])

    def test_enum_rejects_unlisted_value(self) -> None:
        self.assertTrue(js.validate("c", {"enum": ["a", "b"]}))

    def test_enum_distinguishes_bool_from_int(self) -> None:
        self.assertTrue(js.validate(True, {"enum": [1]}))
        self.assertEqual(js.validate(True, {"enum": [True]}), [])


class StringLengthTests(unittest.TestCase):
    def test_min_max_length(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 4}
        self.assertTrue(js.validate("a", schema))
        self.assertTrue(js.validate("abcde", schema))
        self.assertEqual(js.validate("abc", schema), [])


class SupportedTests(unittest.TestCase):
    def test_fully_supported_schema_returns_empty(self) -> None:
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"],
            "additionalProperties": False,
        }
        self.assertEqual(js.supported(schema), [])

    def test_unknown_top_level_keyword_flagged(self) -> None:
        self.assertEqual(js.supported({"type": "string", "pattern": "^a"}), ["pattern"])

    def test_unknown_nested_keyword_flagged(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "number", "minimum": 0}}}
        self.assertEqual(js.supported(schema), ["minimum"])

    def test_additional_properties_schema_form_flagged(self) -> None:
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        self.assertEqual(js.supported(schema), ["additionalProperties(schema)"])

    def test_tuple_items_flagged(self) -> None:
        schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
        self.assertEqual(js.supported(schema), ["items(array)"])

    def test_metadata_keywords_are_not_flagged(self) -> None:
        schema = {"type": "string", "title": "Name", "description": "a name", "$schema": "..."}
        self.assertEqual(js.supported(schema), [])

    def test_items_object_schema_recursed(self) -> None:
        schema = {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "number", "minimum": 0}}}}
        self.assertEqual(js.supported(schema), ["minimum"])


class MutationCheckHelperTests(unittest.TestCase):
    """is_valid is a thin wrapper; exercised for its own sake since flows.py uses it."""

    def test_is_valid_true_and_false(self) -> None:
        self.assertTrue(js.is_valid("x", {"type": "string"}))
        self.assertFalse(js.is_valid(3, {"type": "string"}))


if __name__ == "__main__":
    unittest.main()
