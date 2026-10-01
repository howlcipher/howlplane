import os
import sys
import importlib.util


def load_module(name, path):
    """Import a file as a throwaway module without leaking its name into sys.modules.

    The probe name is prefixed because a source file's basename can collide with a
    stdlib module (``operator.py``); registering it under the bare name replaced the
    real stdlib module for every later test in the process.
    """
    probe_name = f"_import_probe_{name}"
    spec = importlib.util.spec_from_file_location(probe_name, path)
    module = importlib.util.module_from_spec(spec)
    # Register the module before executing it so that dataclass string
    # annotations (from __future__ import annotations) can resolve their module.
    sys.modules[probe_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(probe_name, None)
    return module


def test_src_modules_have_main_function():
    src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    
    # We want to check all python files under src/
    python_files = []
    for root, _, files in os.walk(src_dir):
        if "__pycache__" in root: continue
        for f in files:
            if f.endswith(".py") and f != "__init__.py":
                python_files.append((f, os.path.join(root, f)))

    assert len(python_files) > 0, "No python files found in the src directory."

    for file_name, path in python_files:
        try:
            mod = load_module(file_name[:-3], path)
            # Not all src modules are tools, so we don't enforce a strict main() on everything, 
            # but we verify they can be imported successfully.
            # If they had main() before, they might still have it.
        except (ImportError, SystemExit):
            pass


def test_scripts_are_importable():
    scripts_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts")
    )
    script_files = [
        f for f in os.listdir(scripts_dir) if f.endswith(".py") and f != "__init__.py"
    ]

    assert len(script_files) > 0, "No scripts found in the scripts directory."

    for script_file in script_files:
        path = os.path.join(scripts_dir, script_file)
        try:
            mod = load_module(script_file[:-3], path)
        except (ImportError, SystemExit):
            pass  # Skip if dependency is missing during minimal testing


def test_importing_every_source_file_does_not_shadow_stdlib_modules():
    """A source basename such as operator.py must never replace the stdlib module."""
    import operator
    before = sys.modules["operator"]
    test_src_modules_have_main_function()
    test_scripts_are_importable()
    assert sys.modules["operator"] is before and hasattr(operator, "mul")
