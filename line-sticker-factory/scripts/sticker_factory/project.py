"""Versioned project manifests and immutable image assets.

All mutators return independent snapshots. Saving errors propagate to the caller;
an in-memory snapshot is never evidence of a successful disk write.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

VALID_COUNTS = (8, 16, 24, 32, 40)
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 16_000_000
Manifest = dict[str, object]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def canonical_hash(project: dict) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in project.items()
                                         if k != 'manifestHash'})).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
        raise ValueError('Invalid relative path')
    posix = PurePosixPath(relative)
    if posix.is_absolute() or PureWindowsPath(relative).drive or any(x in ('', '.', '..') for x in relative.split('/')):
        raise ValueError('Path must remain inside the project')
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError('Project directory cannot be a symbolic link')
    target = root
    for part in posix.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('Symbolic links are not allowed in project paths')
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes project directory')
    return target


def validate_project(project: dict, root: Path | None = None) -> None:
    if not isinstance(project, dict) or project.get('schemaVersion') != 1:
        raise ValueError('Unsupported project schema')
    if not isinstance(project.get('projectId'), str) or not project['projectId']:
        raise ValueError('Missing projectId')
    revision = project.get('revision')
    if type(revision) is not int or revision < 1:
        raise ValueError('Invalid revision')
    count = project.get('target', {}).get('count')
    if type(count) is not int or count not in VALID_COUNTS:
        raise ValueError('Invalid LINE sticker count')
    slots = project.get('slots')
    if not isinstance(slots, list) or len(slots) != count:
        raise ValueError('Slot count must match target')
    if any(not isinstance(s, dict) or not isinstance(s.get('slotId'), str) or not s['slotId'] for s in slots):
        raise ValueError('Invalid slotId')
    if len({s['slotId'] for s in slots}) != count:
        raise ValueError('Duplicate slotId')
    ordinals = [s.get('ordinal') for s in slots]
    if any(type(n) is not int for n in ordinals) or sorted(ordinals) != list(range(1, count + 1)):
        raise ValueError('Ordinals must be unique and cover the target count')
    for field in ('attempts', 'assets', 'recipes', 'reviews', 'packages'):
        if not isinstance(project.get(field), list):
            raise ValueError(f'{field} must be an array')
    ids, paths = set(), set()
    for asset in project['assets']:
        if not isinstance(asset, dict) or not asset.get('assetId') or asset['assetId'] in ids:
            raise ValueError('Invalid or duplicate assetId')
        ids.add(asset['assetId'])
        relative = asset.get('relativePath')
        safe_path(root or Path('/project-validation'), relative)
        if relative == 'project.json' or relative in paths:
            raise ValueError('Duplicate or reserved asset path')
        paths.add(relative)
        digest = asset.get('sha256', '')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Asset must have a SHA-256 hash')
        if type(asset.get('byteLength')) is not int or asset['byteLength'] < 0:
            raise ValueError('Invalid asset byteLength')
    canonical_json(project)  # Reject non-JSON values and NaN before writing anything.


def create_project(count: int, brief: dict) -> dict:
    if type(count) is not int or count not in VALID_COUNTS:
        raise ValueError('Choose 8, 16, 24, 32, or 40 stickers')
    project = dict(schemaVersion=1, projectId=str(uuid4()), revision=1,
                   parentRevision=None, brief=copy.deepcopy(brief), target={'count': count},
                   status='planned', updatedAt=now(), attempts=[], assets=[], recipes=[], reviews=[], packages=[])
    project['slots'] = [dict(slotId=str(uuid4()), ordinal=i, caption='', action='', useCase='',
                             planRevision=1, selectedAttemptId=None, recipeId=None, status='planned')
                        for i in range(1, count + 1)]
    return project


def load_project(root: Path) -> dict:
    path = safe_path(root, 'project.json')
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('Project manifest is too large')
    with path.open(encoding='utf-8') as handle:
        project = json.load(handle)
    validate_project(project, root)
    return project


def save_project(root: Path, project: dict) -> None:
    root = Path(root)
    validate_project(project, root)
    target = safe_path(root, 'project.json')
    payload = canonical_json(project)
    if len(payload) > MAX_SOURCE_BYTES:
        raise ValueError('Project manifest is too large')
    root.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix='.project-', suffix='.tmp', delete=False) as handle:
            pending = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, target)
    finally:
        if pending and pending.exists():
            pending.unlink()


def register_asset(root: Path, path: Path, kind: str) -> dict:
    source = Path(path)
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('Source exceeds 20 MiB')
    data = source.read_bytes()
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError('Source exceeds 20 MiB')
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if width * height > MAX_SOURCE_PIXELS:
                raise ValueError('Source exceeds 16 million pixels')
            if getattr(im, 'n_frames', 1) != 1:
                raise ValueError('Only static images are supported')
            fmt = im.format
            if fmt not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Supported sources: PNG, JPEG, WEBP')
            im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Source image cannot be decoded') from exc
    digest = hashlib.sha256(data).hexdigest()
    ext = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}[fmt]
    relative = f'sources/{digest}.{ext}'
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.asset-', delete=False) as handle:
            pending = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(pending, target)  # Atomic publish without replacing an existing source.
        except FileExistsError:
            if target.read_bytes() != data:
                raise ValueError('Existing immutable asset is damaged')
    finally:
        if pending and pending.exists():
            pending.unlink()
    return dict(assetId=digest, kind=kind, sha256=digest, mime=Image.MIME[fmt], width=width,
                height=height, byteLength=len(data), relativePath=relative, createdAt=now())


def _advance(project: dict) -> None:
    project['parentRevision'] = project['revision']
    project['revision'] += 1
    project['updatedAt'] = now()
    project.pop('manifestHash', None)
    for package in project['packages']:
        package['status'] = 'stale'
        package.setdefault('validation', {})['valid'] = False


def _embedded_caption_history(project: dict, slot: dict) -> dict:
    history = slot.setdefault('embeddedCaptions', {})
    if not isinstance(history, dict):
        raise ValueError('Invalid embedded caption history')
    previous = next((a for a in project['assets']
                     if a['assetId'] == slot.get('embeddedCaptionSourceId')), None)
    if previous and isinstance(slot.get('embeddedCaption'), str):
        history.setdefault(previous['sha256'], slot['embeddedCaption'])
    return history


def _lock_integrated_source(project: dict, slot: dict, asset: dict,
                            expected_caption: str | None = None) -> bool:
    before = copy.deepcopy(slot)
    history = _embedded_caption_history(project, slot)
    source_hash = asset['sha256']
    if expected_caption is not None and history.get(source_hash) and history[source_hash] != expected_caption:
        raise ValueError('Known source lettering cannot be relabeled; import a different source for changed text')
    if expected_caption and source_hash in history and not history[source_hash]:
        # Empty wording means unrecorded metadata, not evidence that the image
        # is text-free. It can be filled explicitly once without altering bytes.
        history[source_hash] = expected_caption
    if source_hash not in history:
        active = next((a for a in project['assets'] if a['assetId'] == slot.get('sourceAssetId')), None)
        if expected_caption is None and slot.get('captionLocked') and (active is None or active['sha256'] != source_hash):
            raise ValueError('A different integrated source needs an explicit expected caption; provide expectedCaption or --expected-caption')
        # A new result may carry explicit expected wording. Otherwise only the
        # current editable plan is evidence; another source's wording is not.
        legacy = slot.get('legacy')
        current_text = slot.get('caption', '') or (legacy.get('originalCaption', '') if isinstance(legacy, dict) else '')
        history[source_hash] = expected_caption if expected_caption is not None else current_text
    slot.update(caption='', captionLocked=True, imageContainsCaption=True,
                embeddedCaption=history[source_hash], embeddedCaptionSourceId=asset['assetId'])
    if isinstance(slot.get('legacy'), dict):
        slot['legacy']['captionEditable'] = False
    recipe = next((r for r in project['recipes'] if r.get('recipeId') == slot.get('recipeId')), None)
    recipe_changed = False
    if recipe is not None:
        layer = recipe.setdefault('captionLayer', {})
        if layer.get('enabled') is not False or layer.get('text') != '':
            layer.update(enabled=False, text='')
            recipe['revision'] = recipe.get('revision', 0) + 1
            recipe_changed = True
    changed = before != slot or recipe_changed
    if changed:
        slot.update(status='generated', renderAssetId=None, baseAssetId=None)
        slot.pop('renderHash', None)
        slot.pop('renderFingerprint', None)
    return changed


def record_result(project: dict, attempt_id: str, asset: dict) -> dict:
    result = copy.deepcopy(project)
    attempt = next((a for a in result['attempts'] if a.get('attemptId') == attempt_id), None)
    if attempt is None:
        raise ValueError('Unknown attempt')
    existing = next((a for a in result['assets'] if a['sha256'] == asset['sha256']), None)
    chosen = existing or copy.deepcopy(asset)
    succeeded = attempt.get('status') == 'succeeded'
    if succeeded and chosen['assetId'] not in attempt.get('sourceAssetIds', []):
        raise ValueError('This attempt already has a different result; create a new attempt')
    slot_ids = attempt.get('slotIds', [])
    if not slot_ids or not set(slot_ids).issubset({s['slotId'] for s in result['slots']}):
        raise ValueError('Attempt contains unknown slots')
    if existing is None:
        result['assets'].append(chosen)
    # This describes immutable image bytes, not the crop or the editable layer.
    # Once a source is declared to contain lettering, importing it without the
    # flag cannot make the lettering disappear.
    if attempt.get('imageContainsCaption') is True:
        chosen['imageContainsCaption'] = True
    attempt.update(status='succeeded', sourceAssetIds=[chosen['assetId']], error=None)
    expected_caption = attempt.get('expectedCaption')
    if expected_caption is not None and not isinstance(expected_caption, str):
        raise ValueError('Expected caption must be a string')
    affected = set()
    for slot in result['slots']:
        if not succeeded and slot['slotId'] in slot_ids:
            if slot.get('embeddedCaptionSourceId'):
                _embedded_caption_history(result, slot)
            legacy = slot.get('legacy')
            was_flattened = isinstance(legacy, dict) and (
                legacy.get('sourceField') == 'imageData' or slot.get('captionLocked'))
            embedded = chosen.get('imageContainsCaption') is True
            locked = embedded
            if was_flattened:
                original = next((a for a in result['assets']
                                 if a['assetId'] == legacy.get('originalAssetId')), None)
                # A crop or a new attempt ID cannot prove baked text was removed.
                # Keep the lock if provenance is missing; only distinct bytes unlock.
                locked = locked or original is None or original['sha256'] == chosen['sha256']
            elif slot.get('captionLocked') and not slot.get('imageContainsCaption'):
                original = next((a for a in result['assets']
                                 if a['assetId'] == slot.get('sourceAssetId')), None)
                locked = locked or original is None or original['sha256'] == chosen['sha256']
            if embedded:
                _lock_integrated_source(result, slot, chosen, expected_caption)
            elif not locked and slot.get('imageContainsCaption') and not slot.get('caption'):
                # Keep the intended wording available when a different clean
                # source makes an editable text layer possible again.
                slot['caption'] = slot.get('embeddedCaption', '')
            if locked and slot.get('caption'):
                if isinstance(legacy, dict):
                    legacy['replacementCaption'] = slot['caption']
                slot['caption'] = ''
            slot.update(selectedAttemptId=attempt_id, sourceAssetId=chosen['assetId'], status='generated')
            slot.pop('renderHash', None)
            slot.update(recipeId=None, renderAssetId=None, baseAssetId=None)
            slot['captionLocked'] = locked
            slot['imageContainsCaption'] = embedded
            if isinstance(slot.get('legacy'), dict):
                slot['legacy']['captionEditable'] = not locked
            affected.add(slot['slotId'])
    if chosen.get('imageContainsCaption') is True:
        # The declaration is about shared immutable bytes. Every active slot
        # using those bytes must stop overlaying text, including on an otherwise
        # idempotent attempt replay. Per-slot wording remains separate.
        for slot in result['slots']:
            active = next((a for a in result['assets'] if a['assetId'] == slot.get('sourceAssetId')), None)
            if active and active['sha256'] == chosen['sha256']:
                expected = expected_caption if slot['slotId'] in slot_ids else None
                if _lock_integrated_source(result, slot, chosen, expected):
                    affected.add(slot['slotId'])
    for review in result['reviews']:
        if review.get('slotId') in affected:
            review.update(visualStatus='pending', technicalResults=[], reasons=['Source changed'])
    if result != project:
        _advance(result)
    return result


def revise_caption(project: dict, slot_id: str, caption: str) -> dict:
    result = copy.deepcopy(project)
    slot = next((s for s in result['slots'] if s['slotId'] == slot_id), None)
    if slot is None or not isinstance(caption, str):
        raise ValueError('Unknown slot or invalid caption')
    if slot.get('captionLocked') and caption:
        raise ValueError('Flattened source contains integrated text; regenerate it or import a different clean source before editing text')
    if slot['caption'] == caption:
        return result
    slot.update(caption=caption, status='stale')
    slot.pop('renderHash', None)
    for recipe in result['recipes']:
        if recipe.get('recipeId') == slot.get('recipeId'):
            recipe.setdefault('captionLayer', {})['text'] = caption
            recipe['revision'] = recipe.get('revision', 0) + 1
    for review in result['reviews']:
        if review.get('slotId') == slot_id:
            review.update(visualStatus='pending', technicalResults=[], reasons=['Caption changed'])
    _advance(result)
    return result
