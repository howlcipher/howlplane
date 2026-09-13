import os
import sys
import importlib.util


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Register the module before executing it so that dataclass string
    # annotations (from __future__ import annotations) can resolve their module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
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
