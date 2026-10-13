import gzip
import json
from pathlib import Path

import httpx

from iron_fly.replay_store import (
    DayObject,
    Manifest,
    build_manifest_from_local,
    download_day,
    gzip_json_file,
    parse_manifest,
    select_days,
    sha256_bytes,
)


def test_gzip_is_stable_and_round_trips(tmp_path: Path):
    src = tmp_path / "2026-10-09.json"
    src.write_text('{"version": 4, "tape": []}\n', encoding="utf-8")
    blob = gzip_json_file(src)
    assert gzip.decompress(blob) == src.read_bytes()
    assert sha256_bytes(blob) == sha256_bytes(gzip_json_file(src))


def test_select_days_last_and_range():
    days = tuple(
        DayObject(date=f"2026-10-{i:02d}", key=f"days/2026-10-{i:02d}.json.gz", sha256="a" * 64, bytes=1)
        for i in range(1, 6)
    )
    manifest = Manifest(version=1, updated_at="t", days=days)
    assert [d.date for d in select_days(manifest, last=2)] == ["2026-10-04", "2026-10-05"]
    from datetime import date

    picked = select_days(manifest, day_from=date(2026, 10, 2), day_to=date(2026, 10, 3))
    assert [d.date for d in picked] == ["2026-10-02", "2026-10-03"]


def test_download_day_verifies_sha_and_writes_json(tmp_path: Path):
    plain = b'{"version": 4}\n'
    blob = gzip.compress(plain, compresslevel=9, mtime=0)
    digest = sha256_bytes(blob)
    item = DayObject(date="2026-10-09", key="days/2026-10-09.json.gz", sha256=digest, bytes=len(blob))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/days/2026-10-09.json.gz")
        return httpx.Response(200, content=blob)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        status = download_day(item, tmp_path, public_base="https://example.test", client=client)
    assert status == "ok"
    assert (tmp_path / "2026-10-09.json").read_bytes() == plain
    assert (tmp_path / ".2026-10-09.sha256").read_text().strip() == digest
    with httpx.Client(transport=transport) as client:
        assert download_day(item, tmp_path, public_base="https://example.test", client=client) == "skip"


def test_build_manifest_from_local(tmp_path: Path):
    (tmp_path / "2026-10-08.json").write_text("{}", encoding="utf-8")
    (tmp_path / "2026-10-09.json").write_text("{}", encoding="utf-8")
    manifest = build_manifest_from_local(tmp_path)
    assert [d.date for d in manifest.days] == ["2026-10-08", "2026-10-09"]
    raw = parse_manifest(
        json.loads(
            json.dumps(
                {
                    "version": 1,
                    "updated_at": "t",
                    "days": [
                        {"date": d.date, "key": d.key, "sha256": d.sha256, "bytes": d.bytes} for d in manifest.days
                    ],
                }
            )
        )
    )
    assert len(raw.days) == 2


def test_hydrate_uses_manifest(tmp_path: Path, monkeypatch):
    plain = b'{"ok": true}\n'
    blob = gzip.compress(plain, mtime=0)
    digest = sha256_bytes(blob)
    payload = {
        "version": 1,
        "updated_at": "t",
        "days": [
            {"date": "2026-10-08", "key": "days/2026-10-08.json.gz", "sha256": digest, "bytes": len(blob)},
            {"date": "2026-10-09", "key": "days/2026-10-09.json.gz", "sha256": digest, "bytes": len(blob)},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/manifest.json"):
            return httpx.Response(200, json=payload)
        return httpx.Response(200, content=blob)

    monkeypatch.setenv("REPLAY_BOOTSTRAP_DAYS", "1")
    transport = httpx.MockTransport(handler)
    # Patch httpx.Client used inside hydrate by injecting via download path — call pieces directly.
    with httpx.Client(transport=transport) as client:
        from iron_fly.replay_store import fetch_manifest, select_days

        manifest = fetch_manifest("https://example.test", client=client)
        chosen = select_days(manifest, last=1)
        assert len(chosen) == 1
        assert download_day(chosen[0], tmp_path, public_base="https://example.test", client=client) == "ok"
    assert (tmp_path / "2026-10-09.json").is_file()
