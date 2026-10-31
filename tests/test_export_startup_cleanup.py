from pathlib import Path

import chatgpt_oracle.exports as exports
from chatgpt_oracle.exports import ExportStore


def test_inaccessible_abandoned_snapshot_does_not_block_normal_user_restart(tmp_path, monkeypatch, caplog):
    parent = tmp_path / "snapshots"
    protected = parent / "store-401-protected"
    stale = parent / "ingest-402-stale"
    live = parent / "store-403-live"
    unrelated = parent / "other-data"
    for directory in (protected, stale, live, unrelated):
        directory.mkdir(parents=True)
        (directory / "data").write_bytes(b"retained snapshot")

    monkeypatch.setattr(ExportStore, "_process_is_running", staticmethod(lambda pid: pid == 403))
    original_remove = exports.shutil.rmtree

    def remove(path, *args, **kwargs):
        if Path(path) == protected:
            raise PermissionError("simulated Windows elevated-owner ACL")
        return original_remove(path, *args, **kwargs)

    monkeypatch.setattr(exports.shutil, "rmtree", remove)
    store = ExportStore(snapshot_dir=parent)
    try:
        assert protected.is_dir()
        assert not stale.exists()
        assert live.is_dir()
        assert unrelated.is_dir()
        assert "cleanup deferred" in caplog.text
    finally:
        store.close()
