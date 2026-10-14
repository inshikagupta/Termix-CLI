"""Tests for project-aware command routing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from utils.project_detector import ProjectContext, detect_project
from utils.project_router import route_project_query


class ProjectDetectorTests(unittest.TestCase):
    def test_node_scripts_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({
                    "scripts": {
                        "dev": "vite",
                        "test": "vitest run",
                        "lint": "eslint .",
                        "build": "vite build",
                    }
                }),
                encoding="utf-8",
            )
            ctx = detect_project(str(root))
            self.assertEqual(ctx.project_type, "node")
            self.assertIn("dev", ctx.scripts)
            self.assertEqual(ctx.resolve_dev_command(), "npm run dev")
            self.assertEqual(ctx.resolve_test_command(), "npm run test")
            self.assertEqual(ctx.resolve_lint_command(), "npm run lint")
            self.assertEqual(ctx.resolve_build_command(), "npm run build")

    def test_yarn_package_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite"}}),
                encoding="utf-8",
            )
            (root / "yarn.lock").write_text("", encoding="utf-8")
            ctx = detect_project(str(root))
            self.assertEqual(ctx.package_manager, "yarn")
            self.assertEqual(ctx.resolve_dev_command(), "yarn dev")

    def test_python_pytest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("pytest\nfastapi\n", encoding="utf-8")
            ctx = detect_project(str(root))
            self.assertEqual(ctx.project_type, "python")
            self.assertEqual(ctx.framework, "fastapi")
            self.assertEqual(ctx.resolve_test_command(), "pytest")


class ProjectRouterTests(unittest.TestCase):
    def _node_ctx(self) -> ProjectContext:
        return ProjectContext(
            project_type="node",
            package_manager="npm",
            scripts={"dev": "vite", "test": "vitest", "lint": "eslint .", "build": "vite build"},
        )

    def test_run_tests_node(self):
        result = route_project_query("run tests", project_ctx=self._node_ctx())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.commands, ["npm run test"])
        self.assertEqual(result.source, "project")

    def test_start_dev_server(self):
        result = route_project_query("start dev server", project_ctx=self._node_ctx())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.commands, ["npm run dev"])

    def test_lint_project(self):
        result = route_project_query("lint the project", project_ctx=self._node_ctx())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.commands, ["npm run lint"])

    def test_run_named_script(self):
        result = route_project_query("run electron:dev", project_ctx=ProjectContext(
            project_type="node",
            package_manager="npm",
            scripts={"electron:dev": "concurrently vite electron"},
        ))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.commands, ["npm run electron:dev"])

    def test_integration_with_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"scripts": {"dev": "vite", "build": "vite build"}}),
                encoding="utf-8",
            )
            result = route_project_query("run dev", cwd=str(root))
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.commands, ["npm run dev"])

    def test_unknown_project_returns_none(self):
        ctx = ProjectContext(project_type="unknown", package_manager="")
        self.assertIsNone(route_project_query("run tests", project_ctx=ctx))


if __name__ == "__main__":
    unittest.main()
