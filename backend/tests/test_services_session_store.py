"""Cross-host session store tests (plan Phase 5): pack/unpack round trip,
upload/materialize/purge with an injected dict client, failure tolerance,
tar-slip safety on untrusted archives.
"""

import pytest

from app.services import session_store


class DictClient:
    def __init__(self):
        self.blobs = {}

    def put(self, key, data):
        self.blobs[key] = data

    def get(self, key):
        return self.blobs[key]

    def delete(self, key):
        self.blobs.pop(key, None)


def _volume(tmp_path):
    vol = tmp_path / "sessions" / "run-1" / "thread-1"
    (vol / "projects" / "x").mkdir(parents=True)
    (vol / "session.jsonl").write_text('{"msg": 1}\n{"msg": 2}\n')
    (vol / "projects" / "x" / "state.json").write_text("{}")
    return vol


def test_pack_unpack_round_trip(tmp_path):
    vol = _volume(tmp_path)
    blob = session_store.pack(vol)
    dest = tmp_path / "restored"
    session_store.unpack(blob, dest)
    assert (dest / "session.jsonl").read_text() == '{"msg": 1}\n{"msg": 2}\n'
    assert (dest / "projects" / "x" / "state.json").exists()


def test_upload_materialize_purge_cycle(tmp_path):
    vol = _volume(tmp_path)
    client = DictClient()
    assert session_store.upload("run-1", "thread-1", vol, client=client) is True
    assert "run-1/thread-1.tar.gz" in client.blobs
    dest = tmp_path / "other-host"
    assert session_store.materialize("run-1", "thread-1", dest, client=client) is True
    assert (dest / "session.jsonl").exists()
    assert session_store.purge("run-1", "thread-1", client=client) is True
    assert client.blobs == {}
    # purge is idempotent — an absent mirror counts as purged
    assert session_store.purge("run-1", "thread-1", client=client) is True


def test_upload_missing_volume_is_noop(tmp_path):
    assert session_store.upload("r", "l", tmp_path / "nope", client=DictClient()) is False


def test_upload_failure_never_raises(tmp_path):
    class BadClient(DictClient):
        def put(self, key, data):
            raise ConnectionError("blob store down")

    vol = _volume(tmp_path)
    assert session_store.upload("r", "l", vol, client=BadClient()) is False


def test_materialize_miss_returns_false(tmp_path):
    assert session_store.materialize("ghost", "thread", tmp_path / "d",
                                     client=DictClient()) is False


def test_unpack_rejects_tar_slip(tmp_path):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        payload = b"owned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises((ValueError, Exception)):
        session_store.unpack(buf.getvalue(), tmp_path / "dest")
    assert not (tmp_path / "evil.txt").exists()
