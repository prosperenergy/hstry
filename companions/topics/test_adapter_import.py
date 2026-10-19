import unittest

from adapter_import import prepare


class AdapterPreparationTests(unittest.TestCase):
    def test_only_visible_parts_are_copied_and_source_identity_retained(self):
        rows = [{"externalId": "source-id", "createdAt": 100000, "title": "Original title",
            "messages": [
                {"role": "system", "content": "System text"},
                {"role": "user", "content": "Visible question"},
                {"role": "assistant", "content": "unsafe flattened content", "parts": [
                    {"type": "thinking", "text": "Hidden reasoning"},
                    {"type": "tool_call", "text": "Hidden input"},
                    {"type": "text", "text": "Visible answer"}]},
                {"role": "assistant", "content": "Tool-name fallback", "parts": [{"type": "tool_call", "name": "secret"}]},
                {"role": "tool", "content": "Hidden result"},
                {"role": "user", "content": "# AGENTS.md bootstrap"}]}]
        conversations, report = prepare("cowork", "/source", rows)
        self.assertEqual([(m["role"], m["content"]) for m in conversations["source-id"]["messages"]],
                         [("user", "Visible question"), ("assistant", "Visible answer")])
        self.assertEqual((conversations["source-id"]["created_at"], report["excluded_messages"]), (100, 4))
        self.assertEqual(rows[0]["messages"][2]["content"], "unsafe flattened content")

    def test_missing_identity_and_conflicting_copies_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "identity or date"):
            prepare("provider", "/source", [{"createdAt": 1000}])
        row = {"externalId": "same", "createdAt": 1000, "messages": [{"role": "user", "content": "First"}]}
        changed = dict(row, messages=[{"role": "user", "content": "Changed"}])
        with self.assertRaisesRegex(ValueError, "conflicting copies"):
            prepare("provider", "/source", [row, changed])


if __name__ == "__main__":
    unittest.main()
