import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContestReminderConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_astrbot_schema_exposes_all_reminder_settings(self):
        reminder = self.schema["contest_reminder"]
        self.assertEqual(reminder["type"], "object")
        items = reminder["items"]
        self.assertEqual(items["enabled"]["default"], False)
        self.assertEqual(items["group_whitelist"]["type"], "list")
        self.assertEqual(items["group_whitelist"]["default"], [])
        self.assertEqual(items["reminder_times"]["default"], "24 1")
        for key in (
            "include_div2",
            "include_div3",
            "include_div4",
            "include_educational",
            "include_other",
        ):
            self.assertEqual(items[key]["type"], "bool")

    def test_astrbot_schema_exposes_webui_auto_start(self):
        setting = self.schema["webui_auto_start"]
        self.assertEqual(setting["type"], "bool")
        self.assertFalse(setting["default"])


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_is_consistent(self):
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        tree = ast.parse(main_source)
        plugin_class = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CodeforcesHelperPlugin"
        )
        register_call = next(
            decorator
            for decorator in plugin_class.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "register"
        )
        self.assertEqual(register_call.args[-1].value, "1.3.3")
        self.assertIn("Codeforces Helper v1.3.3", main_source)
        self.assertIn("当前版本：`1.3.3`", readme)
        self.assertRegex(metadata, r"(?m)^version: 1\.3\.3$")

    def test_changelog_has_requested_entry(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## 1.3.3", changelog)
        self.assertIn("修复预判 WA/TLE", changelog)
        self.assertIn("## 1.3.2", changelog)
        self.assertIn("修复判题延迟漏记", changelog)
        self.assertIn("## 1.3.1", changelog)
        self.assertIn("添加 CF 比赛订阅提醒（测试）", changelog)
        self.assertIn("优化管理后台启动逻辑，支持随插件重载自动恢复", changelog)

    def test_scheduler_and_sender_are_wired(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("self.refresh_contest_reminder_jobs", source)
        self.assertIn("async def send_contest_reminder", source)
        self.assertIn("group_whitelist", source)


if __name__ == "__main__":
    unittest.main()
