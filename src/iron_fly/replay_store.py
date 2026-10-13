"""Cloudflare R2 replay archive: gzip days, manifest, download, upload."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

MANIFEST_NAME = "manifest.json"
DAYS_PREFIX = "days/"
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json(?:\.gz)?$")


@dataclass(frozen=True)
class DayObject:
    date: str
    key: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Manifest:
    version: int
    updated_at: str
    days: tuple[DayObject, ...]

    def by_date(self) -> dict[str, DayObject]:
        return {item.date: item for item in self.days}


def default_public_base() -> str:
    return os.environ.get(
        "R2_PUBLIC_BASE",
        "https://pub-2b55d3698db74832b9be7d007e3ef2be.r2.dev",
    ).rstrip("/")


def default_replay_dir() -> Path:
    named = os.environ.get("REPLAY_CACHE", "").strip()
    if named:
        return Path(named)
    return Path(__file__).resolve().parents[2] / "replay"


def bootstrap_days() -> int:
    raw = os.environ.get("REPLAY_BOOTSTRAP_DAYS", "10").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 10


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gzip_json_file(path: Path) -> bytes:
    raw = path.read_bytes()
    return gzip.compress(raw, compresslevel=9, mtime=0)


def write_gunzip(data: bytes, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    plain = gzip.decompress(data)
    dest.write_bytes(plain)


def day_key(day: str) -> str:
    return f"{DAYS_PREFIX}{day}.json.gz"


def parse_manifest(raw: dict | list) -> Manifest:
    if isinstance(raw, list):
        # Allow a bare list of day objects.
        days = tuple(_day_object(item) for item in raw)
        return Manifest(version=1, updated_at=_now_iso(), days=_sort_days(days))
    if not isinstance(raw, dict):
        raise ValueError("manifest must be a JSON object")
    rows = raw.get("days") or []
    if not isinstance(rows, list):
        raise ValueError("manifest.days must be a list")
    days = tuple(_day_object(item) for item in rows)
    return Manifest(
        version=int(raw.get("version") or 1),
        updated_at=str(raw.get("updated_at") or _now_iso()),
        days=_sort_days(days),
    )


def manifest_to_dict(manifest: Manifest) -> dict:
    return {
        "version": manifest.version,
        "updated_at": manifest.updated_at,
        "days": [
            {
                "date": item.date,
                "key": item.key,
                "sha256": item.sha256,
                "bytes": item.bytes,
            }
            for item in manifest.days
        ],
    }


def build_manifest_from_local(replay_dir: Path, days: Iterable[str] | None = None) -> Manifest:
    """Gzip each selected local YYYY-MM-DD.json and describe them (no upload)."""
    selected = set(days) if days is not None else None
    items: list[DayObject] = []
    for path in sorted(replay_dir.glob("????-??-??.json")):
        day = path.stem
        if selected is not None and day not in selected:
            continue
        blob = gzip_json_file(path)
        items.append(
            DayObject(
                date=day,
                key=day_key(day),
                sha256=sha256_bytes(blob),
                bytes=len(blob),
            )
        )
    return Manifest(version=1, updated_at=_now_iso(), days=tuple(items))


def select_days(
    manifest: Manifest,
    *,
    last: int | None = None,
    day_from: date | None = None,
    day_to: date | None = None,
    all_days: bool = False,
) -> list[DayObject]:
    rows = list(manifest.days)
    if all_days:
        return rows
    if day_from is not None or day_to is not None:
        out: list[DayObject] = []
        for item in rows:
            d = date.fromisoformat(item.date)
            if day_from is not None and d < day_from:
                continue
            if day_to is not None and d > day_to:
                continue
            out.append(item)
        return out
    if last is not None:
        if last <= 0:
            return []
        return rows[-last:]
    return rows


def fetch_manifest(public_base: str | None = None, *, client: httpx.Client | None = None) -> Manifest:
    base = (public_base or default_public_base()).rstrip("/")
    url = f"{base}/{MANIFEST_NAME}"
    own = client is None
    http = client or httpx.Client(timeout=60.0)
    try:
        response = http.get(url)
        response.raise_for_status()
        return parse_manifest(response.json())
    finally:
        if own:
            http.close()


def download_day(
    item: DayObject,
    replay_dir: Path,
    *,
    public_base: str | None = None,
    client: httpx.Client | None = None,
    force: bool = False,
) -> str:
    """Ensure unpacked JSON exists. Returns 'ok', 'skip', or raises."""
    dest = replay_dir / f"{item.date}.json"
    meta = replay_dir / f".{item.date}.sha256"
    if not force and dest.is_file() and meta.is_file() and meta.read_text().strip() == item.sha256:
        return "skip"
    base = (public_base or default_public_base()).rstrip("/")
    url = urljoin(base + "/", item.key)
    own = client is None
    http = client or httpx.Client(timeout=120.0)
    try:
        response = http.get(url)
        response.raise_for_status()
        blob = response.content
        digest = sha256_bytes(blob)
        if digest != item.sha256:
            raise ValueError(f"{item.date}: sha256 mismatch remote={item.sha256} got={digest}")
        write_gunzip(blob, dest)
        meta.write_text(digest + "\n")
        return "ok"
    finally:
        if own:
            http.close()


def hydrate(
    replay_dir: Path | None = None,
    *,
    last: int | None = None,
    public_base: str | None = None,
    continue_on_error: bool = False,
) -> tuple[int, int, list[str]]:
    """Download selected days. Returns (downloaded, skipped, errors)."""
    root = replay_dir or default_replay_dir()
    root.mkdir(parents=True, exist_ok=True)
    n = bootstrap_days() if last is None else last
    with httpx.Client(timeout=120.0) as client:
        manifest = fetch_manifest(public_base, client=client)
        chosen = select_days(manifest, last=n)
        downloaded = skipped = 0
        errors: list[str] = []
        for item in chosen:
            try:
                status = download_day(item, root, public_base=public_base, client=client)
                if status == "skip":
                    skipped += 1
                else:
                    downloaded += 1
            except Exception as exc:  # noqa: BLE001 — surface per-day failures
                msg = f"{item.date}: {exc}"
                errors.append(msg)
                if not continue_on_error:
                    raise
        return downloaded, skipped, errors


def r2_client_from_env():
    """boto3 S3 client for R2. Requires boto3 and write credentials."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("boto3 is required for upload: pip install boto3") from exc
    account = os.environ.get("R2_ACCOUNT_ID", "").strip()
    key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    endpoint = os.environ.get("R2_ENDPOINT", "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    if not endpoint or not key or not secret:
        raise RuntimeError("set R2_ENDPOINT (or R2_ACCOUNT_ID), R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name=os.environ.get("R2_REGION", "auto"),
        config=Config(signature_version="s3v4"),
    )


def r2_bucket() -> str:
    name = os.environ.get("R2_BUCKET", "askosyrskiy-spx-data").strip()
    if not name:
        raise RuntimeError("R2_BUCKET is empty")
    return name


def upload_days(
    replay_dir: Path,
    *,
    days: Iterable[str] | None = None,
    client=None,
    bucket: str | None = None,
) -> tuple[int, int, Manifest]:
    """Gzip+upload local days and rewrite manifest. Returns (uploaded, skipped, manifest)."""
    s3 = client or r2_client_from_env()
    bucket_name = bucket or r2_bucket()
    selected = set(days) if days is not None else None
    uploaded = skipped = 0
    by_date: dict[str, DayObject] = {}
    try:
        obj = s3.get_object(Bucket=bucket_name, Key=MANIFEST_NAME)
        remote = parse_manifest(json.loads(obj["Body"].read()))
        by_date = dict(remote.by_date())
    except Exception:
        by_date = {}

    for path in sorted(replay_dir.glob("????-??-??.json")):
        day = path.stem
        if selected is not None and day not in selected:
            continue
        blob = gzip_json_file(path)
        digest = sha256_bytes(blob)
        key = day_key(day)
        prior = by_date.get(day)
        if prior is not None and prior.sha256 == digest:
            skipped += 1
            continue
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=blob,
            ContentType="application/gzip",
        )
        uploaded += 1
        by_date[day] = DayObject(date=day, key=key, sha256=digest, bytes=len(blob))

    manifest = Manifest(version=1, updated_at=_now_iso(), days=_sort_days(tuple(by_date.values())))
    body = json.dumps(manifest_to_dict(manifest), indent=2).encode()
    s3.put_object(
        Bucket=bucket_name,
        Key=MANIFEST_NAME,
        Body=body,
        ContentType="application/json",
    )
    return uploaded, skipped, manifest


def _day_object(raw: object) -> DayObject:
    if not isinstance(raw, dict):
        raise ValueError("each day must be an object")
    day = str(raw.get("date") or "").strip()
    if not _DATE_RE.match(f"{day}.json"):
        raise ValueError(f"bad date {day!r}")
    key = str(raw.get("key") or day_key(day)).strip()
    digest = str(raw.get("sha256") or "").strip()
    if len(digest) != 64:
        raise ValueError(f"bad sha256 for {day}")
    size = int(raw.get("bytes") or 0)
    return DayObject(date=day, key=key, sha256=digest, bytes=size)


def _sort_days(days: Iterable[DayObject]) -> tuple[DayObject, ...]:
    return tuple(sorted(days, key=lambda item: item.date))


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
