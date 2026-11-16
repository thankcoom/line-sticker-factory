"""Lossless V1 JSON preservation and conservative static-pack migration.

The original JSON values and image bytes are retained. New projects require a
fresh render and visual review; unknown flattened captions are never re-stamped.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
from uuid import NAMESPACE_URL, UUID, uuid5
from PIL import Image, UnidentifiedImageError

from .backup import MAX_EXPANDED_BYTES, _check_private_data
from .project import (MAX_SOURCE_BYTES, MAX_SOURCE_PIXELS, VALID_COUNTS, canonical_json, create_project,
                      now, save_project)

_IMAGE_FIELDS = {'imageData', 'baseImageData', 'turnaroundImage', 'expressionMatrixImage'}


def _pointer(parts: tuple) -> str:
    return '/' + '/'.join(str(x).replace('~', '~0').replace('/', '~1') for x in parts)


def _characters(document: dict | list) -> tuple[list, tuple]:
    if isinstance(document, list):
        characters, prefix = document, ()
    elif isinstance(document, dict) and 'characters' in document:
        characters, prefix = document['characters'], ('characters',)
    elif isinstance(document, dict) and isinstance(document.get('packs'), list):
        characters, prefix = [document], None
    else:
        raise ValueError('Expected a Character, Character array, or {characters, seriesList} export')
    if not isinstance(characters, list) or any(not isinstance(c, dict) or not isinstance(c.get('packs'), list) for c in characters):
        raise ValueError('Every legacy character must contain a packs array')
    return characters, prefix


def _image_values(value: object, path: tuple = (), image_context: bool = False):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _image_values(child, path + (key,), image_context or key in _IMAGE_FIELDS or key in ('history', 'turnaroundViews'))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _image_values(child, path + (index,), image_context)
    elif isinstance(value, str) and value and (image_context or value.startswith('data:image/')):
        yield path, value


def _decode_image(encoded: str, label: str) -> bytes:
    if encoded.startswith('data:'):
        header, separator, encoded = encoded.partition(',')
        if not separator or not header.startswith('data:image/') or not header.endswith(';base64'):
            raise ValueError(f'Unsupported image data URI at {label}')
    encoded = ''.join(encoded.split())
    if len(encoded) > ((MAX_SOURCE_BYTES + 2) // 3) * 4:
        raise ValueError(f'Legacy image exceeds 20 MiB at {label}')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f'Invalid base64 image at {label}') from exc
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f'Invalid legacy image size at {label}')
    return data


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _preserve_image(stage: Path, data: bytes, label: str) -> dict:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_SOURCE_PIXELS:
                raise ValueError('Legacy image exceeds 16 million pixels')
            image.load()
            width, height, fmt = image.width, image.height, image.format
            static = getattr(image, 'n_frames', 1) == 1
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f'Legacy image cannot be decoded at {label}') from exc
    digest = hashlib.sha256(data).hexdigest()
    extension = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp', 'GIF': 'gif', 'BMP': 'bmp', 'TIFF': 'tiff'}.get(fmt, 'image')
    relative = f'sources/{digest}.{extension}'
    if not (stage / relative).exists():
        _write_exact(stage / relative, data)
    return {'assetId': digest, 'sha256': digest, 'relativePath': relative, 'kind': 'legacy-image',
            'byteLength': len(data), 'width': width, 'height': height, 'mime': Image.MIME.get(fmt, 'application/octet-stream'),
            'createdAt': now(), 'legacyStaticCompatible': static and fmt in ('PNG', 'JPEG', 'WEBP')}


def migrate_legacy(document: dict | list, destination: Path) -> dict:
    """Migrate into a new/empty directory, or leave the destination unchanged.

    Input is parsed JSON: the backup preserves its values, not original whitespace.
    All original images (including history/model sheets/unsupported packs) are
    preserved at top level, and each converted project's backup carries the full
    original JSON plus every image belonging to that character.
    """
    characters, prefix = _characters(document)
    _check_private_data(document)
    original = canonical_json(document)
    if len(original) > MAX_EXPANDED_BYTES:
        raise ValueError('Legacy document exceeds 500 MiB')
    destination = Path(destination).absolute()
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError('Migration destination already exists; choose a new directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=destination.parent, prefix='.legacy-'))
    report = {'status': 'migrated', 'projects': [], 'warnings': [],
              'backup_file': str(destination / 'legacy-backup.json'), 'imageReferences': []}
    try:
        _write_exact(stage / 'legacy-backup.json', original)
        original_hash = hashlib.sha256(original).hexdigest()
        image_refs, assets_by_id = {}, {}
        for parts, encoded in _image_values(document):
            label = _pointer(parts)
            data = _decode_image(encoded, label)
            asset = _preserve_image(stage, data, label)
            image_refs[parts] = asset['assetId']
            assets_by_id.setdefault(asset['assetId'], asset)
            report['imageReferences'].append({'jsonPointer': label, 'assetId': asset['assetId'], 'relativePath': asset['relativePath']})
        for ci, character in enumerate(characters):
            character_path = () if prefix is None else prefix + (ci,)
            char_id = str(character.get('id', f'character-{ci}'))
            char_refs = [{'jsonPointer': _pointer(parts), 'assetId': asset_id}
                         for parts, asset_id in image_refs.items() if parts[:len(character_path)] == character_path]
            for pi, pack in enumerate(character['packs']):
                if not isinstance(pack, dict) or not isinstance(pack.get('stickers'), list):
                    raise ValueError('Every legacy pack must contain a stickers array')
                pack_id = str(pack.get('id', f'pack-{pi}'))
                context = {'characterId': char_id, 'packId': pack_id}
                if pack.get('category', 'sticker') not in (None, 'sticker'):
                    report['warnings'].append({**context, 'code': 'unsupported-category', 'message': '原始資料已保留；此類別尚未轉為一般靜態貼圖。'})
                    continue
                if len(pack['stickers']) not in VALID_COUNTS:
                    report['warnings'].append({**context, 'code': 'unsupported-count', 'message': f"原始 {len(pack['stickers'])} 張已保留；須先整理成 8、16、24、32 或 40 張。"})
                    continue
                pack_path = character_path + ('packs', pi, 'stickers')
                incompatible = False
                for si, sticker in enumerate(pack['stickers']):
                    if not isinstance(sticker, dict) or (pack_path + (si, 'imageData')) not in image_refs:
                        raise ValueError(f'Missing original sticker image at {_pointer(pack_path + (si,))}')
                    for field in ('imageData', 'baseImageData'):
                        asset_id = image_refs.get(pack_path + (si, field))
                        if asset_id and not assets_by_id[asset_id]['legacyStaticCompatible']:
                            incompatible = True
                if incompatible:
                    report['warnings'].append({**context, 'code': 'unsupported-image', 'message': '已保留原始圖片；包含動畫或首版加工器不支援的格式，未轉為一般靜態套組。'})
                    continue
                if len(original) + sum(assets_by_id[aid]['byteLength'] for aid in {ref['assetId'] for ref in char_refs}) > MAX_EXPANDED_BYTES:
                    raise ValueError('Migrated project backup would exceed 500 MiB')
                seed = canonical_json(['line-sticker-v1', ci, char_id, pi, pack_id]).decode()
                project_id = str(uuid5(NAMESPACE_URL, seed))
                root = stage / 'projects' / project_id
                root.mkdir(parents=True)
                p = create_project(len(pack['stickers']), {'name': pack.get('name', '舊貼圖套組'), 'characterName': character.get('name', ''), 'language': 'zh-Hant', 'theme': pack.get('theme', '')})
                p['projectId'] = project_id
                p['legacy'] = {**context, 'documentHash': original_hash, 'sourceFormat': 'v1', 'originalDocumentAssetId': original_hash,
                               'characterIndex': ci, 'packIndex': pi, 'imageReferences': char_refs,
                               'note': '原始圖與資料均保留；需重新加工並逐張視覺驗收。'}
                document_path = 'legacy/legacy-backup.json'
                _write_exact(root / document_path, original)
                p['assets'].append({'assetId': original_hash, 'kind': 'legacy-document', 'sha256': original_hash,
                                    'mime': 'application/json', 'width': 0, 'height': 0,
                                    'byteLength': len(original), 'relativePath': document_path, 'createdAt': now()})
                for asset_id in dict.fromkeys(ref['assetId'] for ref in char_refs):
                    asset = copy.deepcopy(assets_by_id[asset_id])
                    _write_exact(root / asset['relativePath'], (stage / asset['relativePath']).read_bytes())
                    p['assets'].append(asset)
                flattened = []
                for si, sticker in enumerate(pack['stickers']):
                    if not isinstance(sticker, dict):
                        raise ValueError('Legacy sticker must be an object')
                    sticker_path = character_path + ('packs', pi, 'stickers', si)
                    original_asset_id = image_refs.get(sticker_path + ('imageData',))
                    if original_asset_id is None:
                        raise ValueError(f'Missing original sticker image at {_pointer(sticker_path)}')
                    base_asset_id = image_refs.get(sticker_path + ('baseImageData',))
                    caption = sticker.get('captionText') if isinstance(sticker.get('captionText'), str) else sticker.get('defaultCaption', '')
                    if not isinstance(caption, str):
                        raise ValueError('Legacy caption must be text')
                    slot = p['slots'][si]
                    slot_id = str(uuid5(UUID(project_id), f"slot:{si}:{sticker.get('id', si)}"))
                    recipe_id, attempt_id = 'legacy-recipe-' + slot_id, 'legacy-attempt-' + slot_id
                    slot.update(slotId=slot_id, caption=caption if base_asset_id else '',
                                action=sticker.get('prompt') or sticker.get('emotion') or '', useCase=pack.get('theme', ''),
                                selectedAttemptId=attempt_id, recipeId=recipe_id, sourceAssetId=base_asset_id or original_asset_id,
                                status='generated', captionLocked=base_asset_id is None,
                                legacy={'stickerId': str(sticker.get('id', si)), 'originalCaption': caption,
                                        'captionEditable': base_asset_id is not None, 'originalAssetId': original_asset_id,
                                        'sourceField': 'baseImageData' if base_asset_id else 'imageData'})
                    p['attempts'].append({'attemptId': attempt_id, 'jobId': attempt_id, 'slotIds': [slot_id],
                                          'planRevision': 1, 'executor': 'legacy-v1-import', 'prompt': slot['action'],
                                          'referenceAssetIds': [], 'status': 'succeeded', 'sourceAssetIds': [slot['sourceAssetId']], 'error': None})
                    p['recipes'].append({'recipeId': recipe_id, 'sourceAssetId': slot['sourceAssetId'], 'revision': 1,
                                         'captionLayer': {'text': slot['caption'], 'enabled': base_asset_id is not None}, 'crop': None,
                                         'background': {'mode': 'preserve-alpha'}, 'target': {'width': 370, 'height': 320, 'margin': 10}})
                    if not base_asset_id:
                        flattened.append(si + 1)
                save_project(root, p)
                if flattened:
                    report['warnings'].append({**context, 'code': 'flattened-captions',
                                               'message': f'第 {", ".join(map(str, flattened))} 張沒有已知無字母圖，保留原樣，禁止直接疊加台詞；請匯入乾淨來源後改字。'})
                report['projects'].append({**context, 'projectId': project_id,
                                            'project_dir': str(destination / 'projects' / project_id),
                                            'count': len(p['slots']), 'flattenedSlots': flattened})
        _write_exact(stage / 'migration-report.json', canonical_json(report))
        os.replace(stage, destination)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)
