import logging

from howlplane.control_plane.durable_store import DurableObjectStore


def test_list_all_quarantines_corrupt_files_and_keeps_good(tmp_path, caplog):
    store = DurableObjectStore(tmp_path, factory=lambda d: d)
    store.save("good", {"a": 1})
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "bad.json").write_text("{oops", encoding="utf-8")
    seen = []
    with caplog.at_level(logging.WARNING):
        objs = store.list_all(on_corrupt=lambda p, q, e: seen.append((p.name, q)))
    assert objs == [{"a": 1}]
    assert sorted(n for n, _ in seen) == ["bad.json", "empty.json"]
    assert all(q is not None and q.exists() for _, q in seen)
    assert not (tmp_path / "bad.json").exists()
    assert (list(tmp_path.glob("bad.json.corrupt-*"))[0]).read_text() == "{oops"
    assert "skipping unreadable" in caplog.text
    # second pass is clean and the API without callback still works
    assert store.list_all() == [{"a": 1}]


def test_list_all_skips_factory_failures(tmp_path):
    def factory(d):
        return d["k"]
    store = DurableObjectStore(tmp_path, factory=factory)
    store.save("ok", {"k": 1})
    store.save("nok", {"z": 1})
    assert store.list_all() == [1]
    assert list(tmp_path.glob("nok.json.corrupt-*"))
