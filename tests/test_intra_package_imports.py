"""Static check that every relative import inside the integration resolves.

PR #62 shipped a `camera.py` importing four names its own `const.py` never
defined (`CONF_MAP_ROTATION`, `CONF_MAP_ZOOM`, `MAP_ROTATION_DEFAULT`,
`MAP_ZOOM_DEFAULT`). The whole config entry failed with an ImportError the
moment Home Assistant forwarded the camera platform, but CI stayed green:
no test imports `camera.py`, and `ha_stubs.py` fakes the `homeassistant`
namespace so a real import is never attempted.

This walks the AST instead of importing, so it needs neither Home Assistant
nor the stubs, and it covers every module whether or not a test exercises it.
"""

from __future__ import annotations

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "narwal"


def _toplevel_names(path: pathlib.Path) -> set[str] | None:
    """Names a module binds at module scope, including inside try/except."""
    if not path.exists():
        return None
    names: set[str] = set()

    def collect(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
                names.add(node.name.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Try):
                # `try: from x import Y / except ImportError: Y = None` still binds Y
                collect(node.body)
                for handler in node.handlers:
                    collect(handler.body)
                collect(node.orelse)
                collect(node.finalbody)

    collect(ast.parse(path.read_text(encoding="utf-8")).body)
    return names


def _resolve(source: pathlib.Path, node: ast.ImportFrom) -> pathlib.Path | None:
    """Map a relative ImportFrom onto the file it refers to."""
    base = source.parent
    for _ in range(node.level - 1):
        base = base.parent
    module = node.module or ""
    if not module:
        return base / "__init__.py"
    as_file = base / (module.replace(".", "/") + ".py")
    if as_file.exists():
        return as_file
    as_pkg = base / module.replace(".", "/") / "__init__.py"
    return as_pkg if as_pkg.exists() else None


def test_relative_imports_resolve() -> None:
    """Every `from .x import Y` names something `x` actually defines."""
    exports: dict[pathlib.Path, set[str] | None] = {}
    missing: list[str] = []

    for source in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            target = _resolve(source, node)
            if target is None:
                missing.append(
                    f"{source.relative_to(PACKAGE)}:{node.lineno} "
                    f"imports from unresolvable module '{node.module}'"
                )
                continue
            if target not in exports:
                exports[target] = _toplevel_names(target)
            available = exports[target]
            if available is None:
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in available:
                    missing.append(
                        f"{source.relative_to(PACKAGE)}:{node.lineno} "
                        f"imports '{alias.name}' from '{node.module or '.'}' "
                        f"which does not define it"
                    )

    assert not missing, "unresolvable relative imports:\n  " + "\n  ".join(missing)
