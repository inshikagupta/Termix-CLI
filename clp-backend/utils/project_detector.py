"""
Project context detector.

Scans a directory and identifies what kind of project it is.
Used by the /resolve endpoint to give the LLM better context
and to prioritise relevant KB rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ProjectContext:
    project_type: str                  # "node", "python", "rust", "go", "unknown"
    package_manager: str               # "npm", "pip", "cargo", "go", ""
    has_git: bool = False
    has_docker: bool = False
    framework: str = ""                # "react", "express", "django", "fastapi", ...
    files_found: List[str] = field(default_factory=list)
    scripts: Dict[str, str] = field(default_factory=dict)   # package.json / Makefile targets
    test_runner: str = ""              # pytest, unittest, jest, cargo, go, ...
    makefile_targets: List[str] = field(default_factory=list)

    def to_prompt_string(self) -> str:
        """Human-readable summary for LLM system prompt injection."""
        parts = []
        if self.project_type != "unknown":
            parts.append(f"Project type: {self.project_type}")
        if self.package_manager:
            parts.append(f"Package manager: {self.package_manager}")
        if self.framework:
            parts.append(f"Framework: {self.framework}")
        if self.has_git:
            parts.append("Git repo: yes")
        if self.has_docker:
            parts.append("Docker: yes")
        if self.scripts:
            parts.append(f"Scripts: {', '.join(sorted(self.scripts.keys())[:8])}")
        return " | ".join(parts) if parts else "Unknown project type"

    def run_prefix(self) -> str:
        """Shell prefix for running a package.json script."""
        pm = self.package_manager
        if pm == "yarn":
            return "yarn"
        if pm == "pnpm":
            return "pnpm run"
        return "npm run"

    def script_command(self, *candidates: str) -> Optional[str]:
        """Return a shell command for the first script name found in the project."""
        for name in candidates:
            if name in self.scripts:
                return f"{self.run_prefix()} {name}"
        return None

    def resolve_dev_command(self) -> Optional[str]:
        if self.project_type == "node":
            return self.script_command("dev", "start:dev", "serve", "watch", "develop")
        if self.project_type == "python":
            if self.framework == "django" and "manage.py" in self.files_found:
                return "python manage.py runserver"
            if self.framework == "fastapi":
                return self.script_command("dev") or "uvicorn main:app --reload"
            return self.script_command("dev", "serve", "start")
        if self.project_type == "rust":
            return "cargo run"
        if self.project_type == "go":
            return "go run ."
        return self.script_command("dev", "start", "serve")

    def resolve_test_command(self) -> Optional[str]:
        if self.project_type == "node":
            return self.script_command("test", "test:unit", "test:ci", "vitest", "jest")
        if self.project_type == "python":
            if self.test_runner == "pytest":
                return "pytest"
            if self.test_runner == "unittest":
                return "python -m unittest discover"
            return "python -m pytest"
        if self.project_type == "rust":
            return "cargo test"
        if self.project_type == "go":
            return "go test ./..."
        target = self._makefile_target("test")
        if target:
            return f"make {target}"
        return self.script_command("test")

    def resolve_lint_command(self) -> Optional[str]:
        if self.project_type == "node":
            return self.script_command("lint", "lint:fix", "eslint", "check")
        if self.project_type == "python":
            if self.test_runner == "ruff" or "ruff" in self.files_found:
                return "ruff check ."
            return self.script_command("lint") or "ruff check ."
        target = self._makefile_target("lint")
        if target:
            return f"make {target}"
        return self.script_command("lint")

    def resolve_build_command(self) -> Optional[str]:
        if self.project_type == "node":
            return self.script_command("build", "build:prod", "compile")
        if self.project_type == "python":
            return self.script_command("build")
        if self.project_type == "rust":
            return "cargo build"
        if self.project_type == "go":
            return "go build"
        target = self._makefile_target("build")
        if target:
            return f"make {target}"
        return self.script_command("build")

    def _makefile_target(self, name: str) -> Optional[str]:
        if name in self.makefile_targets:
            return name
        return None


# Marker files → (project_type, package_manager)
_MARKERS = {
    "package.json":       ("node",   "npm"),
    "yarn.lock":          ("node",   "yarn"),
    "pnpm-lock.yaml":     ("node",   "pnpm"),
    "requirements.txt":   ("python", "pip"),
    "pyproject.toml":     ("python", "pip"),
    "setup.py":           ("python", "pip"),
    "Pipfile":            ("python", "pipenv"),
    "Cargo.toml":         ("rust",   "cargo"),
    "go.mod":             ("go",     "go"),
    "pom.xml":            ("java",   "maven"),
    "build.gradle":       ("java",   "gradle"),
    "Gemfile":            ("ruby",   "bundler"),
    "composer.json":      ("php",    "composer"),
}

# Framework detection via package.json deps (checked if node project)
_NODE_FRAMEWORKS = {
    "react":       "react",
    "next":        "next.js",
    "vue":         "vue",
    "nuxt":        "nuxt",
    "angular":     "angular",
    "svelte":      "svelte",
    "express":     "express",
    "fastify":     "fastify",
    "nestjs":      "nestjs",
    "vite":        "vite",
    "electron":    "electron",
}

# Python framework detection via imports in known files
_PYTHON_FRAMEWORKS = {
    "django":   "django",
    "fastapi":  "fastapi",
    "flask":    "flask",
    "tornado":  "tornado",
}


def _parse_makefile_targets(makefile: Path) -> List[str]:
    """Extract target names from a Makefile (best-effort)."""
    targets: List[str] = []
    try:
        for line in makefile.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("."):
                continue
            if stripped.startswith(".PHONY:"):
                _, _, rest = stripped.partition(":")
                targets.extend(t.strip() for t in rest.split() if t.strip())
                continue
            if ":" in stripped and not stripped.startswith("\t"):
                name = stripped.split(":", 1)[0].strip()
                if name and not name.startswith("."):
                    targets.append(name)
    except Exception:
        return []
    return list(dict.fromkeys(targets))


def detect_project(cwd: str) -> ProjectContext:
    """
    Scan *cwd* and return a ProjectContext describing the project.
    Never raises — returns an 'unknown' context on any error.
    """
    try:
        return _scan(cwd)
    except Exception:
        return ProjectContext(project_type="unknown", package_manager="")


def _scan(cwd: str) -> ProjectContext:
    path = Path(os.path.expanduser(cwd))
    if not path.is_dir():
        return ProjectContext(project_type="unknown", package_manager="")

    try:
        entries = {e.name for e in path.iterdir()}
    except PermissionError:
        return ProjectContext(project_type="unknown", package_manager="")

    project_type = "unknown"
    package_manager = ""
    framework = ""
    files_found: List[str] = []

    # Identify project type from marker files
    for marker, (ptype, pm) in _MARKERS.items():
        if marker in entries:
            files_found.append(marker)
            if project_type == "unknown":  # first match wins
                project_type = ptype
                package_manager = pm

    has_git = ".git" in entries
    has_docker = any(f in entries for f in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml"))

    scripts: Dict[str, str] = {}
    test_runner = ""
    makefile_targets: List[str] = []

    # Framework detection for Node projects + script extraction
    if project_type == "node" and "package.json" in entries:
        try:
            import json
            pkg = json.loads((path / "package.json").read_text(encoding="utf-8"))
            raw_scripts = pkg.get("scripts") or {}
            if isinstance(raw_scripts, dict):
                scripts = {str(k): str(v) for k, v in raw_scripts.items()}
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
            }
            for dep_key, fw_name in _NODE_FRAMEWORKS.items():
                if any(dep_key in k.lower() for k in all_deps):
                    framework = fw_name
                    break
            dep_names = " ".join(all_deps.keys()).lower()
            if "vitest" in dep_names or "jest" in dep_names:
                test_runner = "jest" if "jest" in dep_names else "vitest"
        except Exception:
            pass

    # Framework + test runner detection for Python projects
    if project_type == "python":
        if "manage.py" in entries:
            files_found.append("manage.py")
        try:
            req_file = path / "requirements.txt"
            if req_file.exists():
                content = req_file.read_text(encoding="utf-8").lower()
                for dep_key, fw_name in _PYTHON_FRAMEWORKS.items():
                    if dep_key in content:
                        framework = fw_name
                        break
                if "pytest" in content:
                    test_runner = "pytest"
                elif "ruff" in content:
                    test_runner = "ruff"
        except Exception:
            pass
        try:
            pyproject = path / "pyproject.toml"
            if pyproject.exists():
                content = pyproject.read_text(encoding="utf-8").lower()
                if "[tool.pytest" in content or "pytest" in content:
                    test_runner = "pytest"
                if "ruff" in content:
                    test_runner = "ruff"
                for dep_key, fw_name in _PYTHON_FRAMEWORKS.items():
                    if dep_key in content and not framework:
                        framework = fw_name
        except Exception:
            pass
        if not test_runner:
            test_runner = "pytest"

    if project_type == "rust":
        test_runner = "cargo"
    if project_type == "go":
        test_runner = "go"

    if "Makefile" in entries:
        makefile_targets = _parse_makefile_targets(path / "Makefile")
        if not scripts:
            for target in ("dev", "test", "lint", "build", "run", "start"):
                if target in makefile_targets:
                    scripts[target] = f"make {target}"

    return ProjectContext(
        project_type=project_type,
        package_manager=package_manager,
        has_git=has_git,
        has_docker=has_docker,
        framework=framework,
        files_found=files_found,
        scripts=scripts,
        test_runner=test_runner,
        makefile_targets=makefile_targets,
    )
