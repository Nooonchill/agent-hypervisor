from __future__ import annotations

import unittest
from dataclasses import replace

from agent_hypervisor.models import HypervisorError
from agent_hypervisor.task_form import DEFAULT_TASK_FORM, resolve_task_context


class TaskFormTests(unittest.TestCase):
    def test_sources_as_free_text_preserve_commas_and_lines(self):
        fields = tuple(
            replace(field, input_type="multiline", default="")
            if field.field_id == "sources"
            else field
            for field in DEFAULT_TASK_FORM
        )
        value = "See the design, especially section 2.\nAsk the team about missing examples."
        self.assertEqual(resolve_task_context(fields, {"sources": value})["sources"], value)
        self.assertEqual(
            resolve_task_context(fields, {"sources": ["a,b.md", "c.md"]})["sources"], "a,b.md\nc.md"
        )

    def test_string_lists_use_lines_without_csv_parsing(self):
        values = resolve_task_context(
            DEFAULT_TASK_FORM, {"sources": "a,b.md\nhttps://example.test/a,b\n\n"}
        )
        self.assertEqual(values["sources"], ("a,b.md", "https://example.test/a,b"))
        with self.assertRaises(HypervisorError):
            resolve_task_context(DEFAULT_TASK_FORM, {"sources": [1]})
