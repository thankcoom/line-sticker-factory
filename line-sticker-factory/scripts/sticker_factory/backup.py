"""Portable, integrity-checked project backups with transactional restore."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from pathlib import Path

from .project import (MAX_SOURCE_BYTES, canonical_hash, canonical_json, load_project,
                      safe_path, save_project, validate_project)

MAX_ENTRIES = 2000
MAX_EXPANDED_BYTES = 500 * 1024 * 1024
_SECRET_KEYS = {'apikey', 'accesskey', 'accesstoken', 'refreshtoken', 'password',
                'authorization', 'cookie', 'secretkey', 'privatekey', 'clientsecret'}


def _check_private_data(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = ''.join(c for c in key.lower() if c.isalnum())
            if normalized in _SECRET_KEYS and item:
                raise ValueError('Credentials cannot be included in a backup')
            _check_private_data(item)
    elif isinstance(value, list):
        for item in value:
            _check_private_data(item)


def _check_assets(root: Path, project: dict) -> None:
    total = 0
    for asset in project['assets']:
        if asset.get('kind') == 'font' and asset.get('redistributionAllowed') is not True:
            raise ValueError('Font redistribution permission has not been confirmed')
        path = safe_path(root, asset['relativePath'])
        if not path.is_file() or path.stat().st_size != asset['byteLength']:
            raise ValueError(f'Missing or damaged asset: {asset["relativePath"]}')
        total += asset['byteLength']
        if total > MAX_EXPANDED_BYTES:
            raise ValueError('Backup exceeds expanded size limit')
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            actual = 0
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                actual += len(chunk)
                if actual > asset['byteLength']:
                    raise ValueError('Asset changed while reading')
                digest.update(chunk)
        if actual != asset['byteLength'] or digest.hexdigest() != asset['sha256']:
            raise ValueError(f'Asset hash mismatch: {asset["relativePath"]}')


def export_backup(project_dir: Path, target_zip: Path) -> dict:
    project_dir, target_zip = Path(project_dir), Path(target_zip)
    project = copy.deepcopy(load_project(project_dir))
    protected = [safe_path(project_dir, 'project.json')]
    protected.extend(safe_path(project_dir, a['relativePath']) for a in project['assets'])
    if any(target_zip.resolve() == path.resolve() for path in protected):
        raise ValueError('Backup cannot replace the manifest or an original asset')
    _check_private_data(project)
    _check_assets(project_dir, project)
    if len(project['assets']) + 1 > MAX_ENTRIES:
        raise ValueError('Too many backup entries')
    project['manifestHash'] = canonical_hash(project)
    payload = canonical_json(project)
    if len(payload) + sum(a['byteLength'] for a in project['assets']) > MAX_EXPANDED_BYTES:
        raise ValueError('Backup exceeds expanded size limit')
    if target_zip.is_symlink():
        raise ValueError('Backup output cannot be a symbolic link')
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_zip.parent, prefix='.backup-', suffix='.zip', delete=False) as handle:
            pending = Path(handle.name)
        with zipfile.ZipFile(pending, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('project.json', payload)
            for asset in project['assets']:
                source = safe_path(project_dir, asset['relativePath'])
                with source.open('rb') as incoming, archive.open(asset['relativePath'], 'w') as outgoing:
                    digest, size = hashlib.sha256(), 0
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                        size += len(chunk)
                        if size > asset['byteLength']:
                            raise ValueError('Asset changed during backup')
                        digest.update(chunk)
                        outgoing.write(chunk)
                    if size != asset['byteLength'] or digest.hexdigest() != asset['sha256']:
                        raise ValueError('Asset changed during backup')
        # Reopen and read every byte; never publish an unverified archive.
        with zipfile.ZipFile(pending) as archive:
            if archive.testzip() is not None:
                raise ValueError('Backup CRC verification failed')
        with pending.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(pending, target_zip)
    finally:
        if pending and pending.exists():
            pending.unlink()
    return {'backup_zip': str(target_zip), 'manifest_hash': project['manifestHash']}


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest field')
        result[key] = value
    return result


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, maximum: int) -> bytes:
    if info.file_size > maximum:
        raise ValueError('Backup member exceeds expanded size limit')
    chunks, size = [], 0
    with archive.open(info) as handle:
        for chunk in iter(lambda: handle.read(min(1024 * 1024, maximum + 1)), b''):
            size += len(chunk)
            if size > maximum or size > info.file_size:
                raise ValueError('Backup member exceeds declared size')
            chunks.append(chunk)
    if size != info.file_size:
        raise ValueError('Backup member size mismatch')
    return b''.join(chunks)


def restore_backup(zip_path: Path, destination: Path) -> dict:
    destination = Path(destination).absolute()
    if destination.is_symlink():
        raise ValueError('Destination cannot be a symbolic link')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=destination.parent, prefix='.restore-'))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES:
                raise ValueError('Too many backup entries')
            if sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                raise ValueError('Backup exceeds expanded size limit')
            names, normalized_names = {}, set()
            for info in infos:
                safe_path(stage, info.filename)
                name_key = unicodedata.normalize('NFC', info.filename).casefold()
                if name_key in normalized_names:
                    raise ValueError('Duplicate backup entry')
                normalized_names.add(name_key)
                if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('Directory and symbolic-link entries are not permitted')
                if info.flag_bits & 1:
                    raise ValueError('Encrypted backup entries are not supported')
                names[info.filename] = info
            if 'project.json' not in names:
                raise ValueError('Backup manifest missing')
            payload = _read_member(archive, names['project.json'], MAX_SOURCE_BYTES)
            try:
                project = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError('Backup manifest is invalid JSON') from exc
            validate_project(project, stage)
            _check_private_data(project)
            if project.get('manifestHash') != canonical_hash(project):
                raise ValueError('Backup manifest hash mismatch')
            expected = {'project.json'} | {a['relativePath'] for a in project['assets']}
            if set(names) != expected:
                raise ValueError('Backup contains missing or unexpected entries')
            for asset in project['assets']:
                info = names[asset['relativePath']]
                if info.file_size != asset['byteLength']:
                    raise ValueError('Backup asset size mismatch')
                target = safe_path(stage, asset['relativePath'])
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(info) as incoming, target.open('xb') as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b''):
                        size += len(chunk)
                        if size > info.file_size or size > MAX_EXPANDED_BYTES:
                            raise ValueError('Backup asset expanded beyond limit')
                        digest.update(chunk)
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if size != info.file_size or digest.hexdigest() != asset['sha256']:
                    raise ValueError('Backup asset hash mismatch')
            _check_assets(stage, project)
            save_project(stage, project)
        if destination.exists():
            if not destination.is_dir():
                raise ValueError('Destination is not a directory')
            if any(destination.iterdir()):
                existing = load_project(destination)
                if (existing['projectId'], existing['revision'], canonical_hash(existing)) == (
                        project['projectId'], project['revision'], project['manifestHash']):
                    _check_assets(destination, existing)
                    return {'status': 'noop', 'project_dir': str(destination)}
                raise ValueError('Project conflict: restore to a new directory or cancel')
        os.replace(stage, destination)
        return {'status': 'restored', 'project_dir': str(destination)}
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Backup archive is damaged or unsupported') from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)
