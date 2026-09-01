import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SourceRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls.backend_tree = ast.parse((ROOT / "backend" / "api.py").read_text(encoding="utf-8"))
        cls.crawler_source = (ROOT / "core" / "crawler.py").read_text(encoding="utf-8")

    def test_webui_commands_require_admin_permission(self):
        functions = {
            node.name: node
            for node in ast.walk(self.main_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("cmd_start_webui", "cmd_stop_webui"):
            decorators = [ast.unparse(item) for item in functions[name].decorator_list]
            self.assertTrue(
                any("permission_type" in item and "ADMIN" in item for item in decorators),
                f"{name} must require administrator permission",
            )

    def test_member_mutations_take_sync_lock(self):
        functions = {
            node.name: node
            for node in ast.walk(self.backend_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        calls = [
            node
            for node in ast.walk(functions["admin_users"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "acquire_sync_lock"
        ]
        self.assertGreaterEqual(len(calls), 2)

        main_functions = {
            node.name: node
            for node in ast.walk(self.main_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        chat_delete_calls = [
            node
            for node in ast.walk(main_functions["cmd_delete_user"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "acquire_sync_lock"
        ]
        self.assertEqual(len(chat_delete_calls), 1)

    def test_submission_write_rechecks_current_handle(self):
        self.assertIn("SELECT cf_handle FROM users WHERE qq_id = ?", self.crawler_source)
        self.assertIn("Handle 在同步期间发生变化", self.crawler_source)


if __name__ == "__main__":
    unittest.main()
