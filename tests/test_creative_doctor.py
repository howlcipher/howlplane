"""`howlplane doctor --creative`: dependency, interpreter and policy preflight."""

import json
from pathlib import Path
import types

import pytest

from howlplane.control_plane import cli, creative_doctor as cd

pytestmark = pytest.mark.unit

PIN = "d0054c46255dbefbf8865baab45d0fc7c9530eba"
STALE = "a3ddc6650000000000000000000000000000beef"


class FakeDist:
    def __init__(self, version, commit=None, requires=()):
        self.version = version
        self.requires = list(requires)
        self._direct = {"url": "https://github.com/x", "vcs_info": {"commit_id": commit}} if commit else {}

    def read_text(self, name):
        return json.dumps(self._direct) if name == "direct_url.json" else None


def env_with(core_commit=PIN, core_symbols=True, writer=True, create=True, schema_match=True,
             tmp=None, writer_pin=PIN):
    core_requirement = f"howl-provider-core @ git+https://github.com/howlcipher/howl-provider-core.git@{PIN}"
    writer_requirement = core_requirement.replace(PIN, writer_pin)
    dists = {
        "howl-provider-core": FakeDist("0.1.0", core_commit),
        "howldream": FakeDist("0.4.4", "d" * 40, [core_requirement]),
        "howlwriter": FakeDist("0.1.0", "e" * 40, [writer_requirement]),
        "howlcreate": FakeDist("0.2.0", "f" * 40, [core_requirement]),
    }
    if not writer:
        dists.pop("howlwriter")

    def lookup(name):
        if name not in dists:
            raise cd.metadata.PackageNotFoundError(name)
        return dists[name]

    schema = {"$id": "howlwriter.copy_package/v1"}
    writer_dir = tmp / "writer" / "schemas"
    create_dir = tmp / "create"
    (create_dir / "schemas").mkdir(parents=True, exist_ok=True)
    writer_dir.mkdir(parents=True, exist_ok=True)
    name = "howlwriter.copy_package.v1.schema.json"
    (writer_dir / name).write_text(json.dumps(schema))
    if create:
        (create_dir / "schemas" / name).write_text(json.dumps(schema if schema_match else {"$id": "old"}))

    def importer(module_name):
        if module_name == "howl_provider_core" and not core_symbols:
            return types.SimpleNamespace(CommandConfig=1, CommandProvider=1, Policy=1, ProviderError=1)
        if module_name.startswith("howlwriter") and not writer:
            raise ImportError(module_name)
        if module_name == "howlwriter.schemas":
            return types.SimpleNamespace(__file__=str(writer_dir / "__init__.py"))
        if module_name == "howlcreate":
            return types.SimpleNamespace(__file__=str(create_dir / "__init__.py"))
        symbols = {s: object() for m in cd.REQUIRED_SYMBOLS.values() for s in m.get(module_name, ())}
        return types.SimpleNamespace(**symbols)

    return lookup, importer


def run(tmp_path, *, env=None, interpreter=None, config=None, which=None, **kw):
    lookup, importer = env_with(tmp=tmp_path, **kw)
    return cd.summarize(cd.run_checks(
        env=env if env is not None else {"HOWL_FORBID_LOCAL_INFERENCE": "1"},
        command_config=config, workspace=tmp_path, lookup=lookup, importer=importer,
        which=which or (lambda name: None),
        interpreter=interpreter or ("/usr/bin/python3", (3, 14, 5), str(tmp_path / "venv"), "/usr")))


def statuses(report):
    return {c["id"]: c["status"] for c in report["checks"]}


def venv(tmp_path, version="3.14.5", home="/usr/bin"):
    (tmp_path / "venv").mkdir(exist_ok=True)
    (tmp_path / "venv" / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = {version}\n")


def test_healthy_environment_has_no_fail(tmp_path):
    venv(tmp_path)
    report = run(tmp_path)
    assert report["counts"]["FAIL"] == 0
    found = statuses(report)
    assert found["compat.howlcreate.core"] == "PASS"
    assert found["compat.writer.create"] == "PASS"
    assert found["policy.local"] == "PASS"


def test_stale_provider_core_missing_classify_failure_fails(tmp_path):
    # Run 5: ImportError: cannot import name 'classify_failure' from howl_provider_core.
    venv(tmp_path)
    report = run(tmp_path, core_commit=STALE, core_symbols=False)
    found = statuses(report)
    assert found["package.howl-provider-core"] == "FAIL"
    assert found["compat.howlcreate.core"] == "FAIL"
    row = next(c for c in report["checks"] if c["id"] == "compat.howlcreate.core")
    assert PIN in row["fix"] and "a3ddc6650000" in row["detail"]
    assert "classify_failure" in next(
        c for c in report["checks"] if c["id"] == "package.howl-provider-core")["detail"]


def test_different_core_commit_with_symbols_is_warning(tmp_path):
    venv(tmp_path)
    assert statuses(run(tmp_path, core_commit=STALE))["compat.howlwriter.core"] == "WARN"


def test_incompatible_writer_create_contract_fails(tmp_path):
    venv(tmp_path)
    assert statuses(run(tmp_path, schema_match=False))["compat.writer.create"] == "FAIL"
    assert statuses(run(tmp_path, create=False))["compat.writer.create"] == "FAIL"


def test_missing_writer_fails(tmp_path):
    venv(tmp_path)
    assert statuses(run(tmp_path, writer=False))["package.howlwriter"] == "FAIL"


@pytest.mark.parametrize("version,home,expected", [
    ("3.13.2", "/usr/bin", "FAIL"),
    ("3.14.5", "/nonexistent/old-home/bin", "FAIL"),
    ("3.14.4", "/usr/bin", "WARN"),
])
def test_stale_interpreter_and_venv(tmp_path, version, home, expected):
    venv(tmp_path, version, home)
    assert statuses(run(tmp_path))["venv"] == expected


def test_stale_entrypoint_shebang_fails(tmp_path):
    venv(tmp_path)
    script = tmp_path / "howlcreate"
    script.write_text("#!/var/home/old/.venv/bin/python\nprint()\n")
    report = run(tmp_path, which=lambda name: str(script) if name == "howlcreate" else None)
    assert statuses(report)["cli.howlcreate"] == "FAIL"


def test_local_model_policy(tmp_path):
    venv(tmp_path)
    assert statuses(run(tmp_path, env={}))["policy.local"] == "WARN"
    unsafe = run(tmp_path, env={"OLLAMA_HOST": "http://127.0.0.1:11434"})
    assert statuses(unsafe)["policy.local_config"] == "FAIL"
    blocked = run(tmp_path, env={"OLLAMA_HOST": "x", "HOWL_FORBID_LOCAL_INFERENCE": "1"})
    assert statuses(blocked)["policy.local_config"] == "WARN"
    remote = run(tmp_path, env={"OPENAI_BASE_URL": "https://api.example.com",
                                "HOWL_FORBID_LOCAL_INFERENCE": "1"})
    assert statuses(remote)["policy.local_config"] == "PASS"


class _FakeCommandConfig:
    """Mirrors provider-core's contract: explicit argv, remote only, no local launchers."""

    def __init__(self, argv, remote):
        if remote is not True or argv[0] in {"ollama", "bash"}:
            raise ValueError("command requires operator-declared remote execution")
        self.argv, self.adapter, self.output_format = tuple(argv), None, "text"

    @classmethod
    def read(cls, path):
        value = json.loads(Path(path).read_text())
        return cls(value["argv"], value.get("remote"))


def test_command_config_validated_without_exposing_values(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "howl_provider_core", types.SimpleNamespace(
        CommandConfig=_FakeCommandConfig, ProviderError=ValueError))
    venv(tmp_path)
    good = tmp_path / "remote.json"
    good.write_text(json.dumps({"argv": ["python3", "--api-key=SECRET123"], "remote": True}))
    report = run(tmp_path, config=good)
    assert statuses(report)["provider.remote"] == "PASS"
    assert "SECRET123" not in json.dumps(report)
    bad = tmp_path / "local.json"
    bad.write_text(json.dumps({"argv": ["ollama", "run"], "remote": True}))
    assert statuses(run(tmp_path, config=bad))["provider.remote"] == "FAIL"


def test_doctor_creative_cli_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cd, "run_checks", lambda **kw: [cd.Check("x", "X", cd.PASS, "fine")])
    assert cli.main(["doctor", "--creative", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "howlplane.doctor.creative/v1"
    assert report["status"] == "PASS"
    monkeypatch.setattr(cd, "run_checks", lambda **kw: [cd.Check("x", "X", cd.FAIL, "broken", "fix it")])
    assert cli.main(["doctor", "--creative"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "Suggested action: fix it" in out


def test_doctor_does_not_mutate_environment(tmp_path):
    venv(tmp_path)
    lookup, importer = env_with(tmp=tmp_path)  # builds the fake component layout
    before = sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    cd.run_checks(env={}, workspace=tmp_path, lookup=lookup, importer=importer,
                  which=lambda name: None,
                  interpreter=("/usr/bin/python3", (3, 14, 5), str(tmp_path / "venv"), "/usr"))
    assert sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before


def test_disagreeing_provider_core_pins_fail(tmp_path):
    # Found in this mission's fresh-environment install: Writer pinned d0054c4 while
    # Create and Dream pinned 5837d46, so pip could not resolve them together.
    venv(tmp_path)
    assert statuses(run(tmp_path))["compat.core_pin_agreement"] == "PASS"
    report = run(tmp_path, writer_pin="5837d467a76e2f9e03b6f32a0feb957f9e48f95e")
    row = next(c for c in report["checks"] if c["id"] == "compat.core_pin_agreement")
    assert row["status"] == "FAIL"
    assert "howlwriter@5837d467a76e" in row["detail"]
