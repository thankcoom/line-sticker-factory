"""Deterministic LINE packaging; technical validation is not a visual review."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile
from PIL import Image, ImageDraw, ImageOps

COUNTS=(8,16,24,32,40)
MAX_IMAGE_BYTES=1_000_000
MAX_ZIP_BYTES=60_000_000


def digest(data:bytes)->str:
    return hashlib.sha256(data).hexdigest()


def render_fingerprint(project:dict, slot:dict)->str:
    recipe=next((r for r in project['recipes'] if r['recipeId']==slot.get('recipeId')),None)
    if recipe is None:raise ValueError('Missing recipe for slot '+str(slot['ordinal']))
    source=next((a for a in project['assets'] if a['assetId']==recipe['sourceAssetId']),None)
    if source is None:raise ValueError('Missing source asset')
    payload={'recipe':recipe,'caption':slot.get('caption',''),'selectedAttemptId':slot.get('selectedAttemptId'),'planRevision':slot.get('planRevision'),'sourceHash':source['sha256']}
    return digest(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())


def validate_png(data:bytes,size:tuple[int,int])->list[str]:
    errors=[]
    if len(data)>MAX_IMAGE_BYTES:return ['Image exceeds 1,000,000 bytes']
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format!='PNG':return ['Image is not PNG']
            if im.size!=size:return [f'Incorrect dimensions: {im.size}, expected {size}']
            if im.mode!='RGBA':errors.append('PNG must use RGBA')
            if getattr(im,'n_frames',1)!=1:errors.append('Animated PNG is not supported by static packs')
            dpi=im.info.get('dpi',(0,0))
            if len(dpi)!=2 or min(dpi)<71.9:errors.append('PNG resolution is below 72 dpi')
            im.load()
            if 'A' not in im.getbands():errors.append('Missing alpha channel')
            else:
                lo,hi=im.getchannel('A').getextrema()
                if hi==0:errors.append('Image is fully transparent')
                if lo!=0:errors.append('Image has no transparent background pixels')
    except Exception as e:errors.append('Cannot decode PNG: '+str(e))
    return errors


def validate_pack(zip_path:Path, expected_count:int, expected_hashes:dict[str,str])->dict:
    errors=[];entries=[]
    if type(expected_count) is not int or expected_count not in COUNTS:return {'valid':False,'errors':['Unsupported sticker count'],'entries':[]}
    expected={'main.png','tab.png'}|{f'{i:02d}.png' for i in range(1,expected_count+1)}
    try:
        if zip_path.stat().st_size>MAX_ZIP_BYTES:raise ValueError('ZIP exceeds 60,000,000 bytes')
        with zipfile.ZipFile(zip_path) as z:
            infos=z.infolist();entries=[i.filename for i in infos]
            if len(entries)!=len(set(entries)):errors.append('Duplicate ZIP entries')
            if set(entries)!=expected:errors.append('ZIP entry names do not match expected root images')
            if errors:return {'valid':False,'errors':errors,'entries':entries}
            for info in infos:
                name=info.filename
                if info.file_size>MAX_IMAGE_BYTES:
                    errors.append(name+': Image exceeds 1,000,000 bytes');continue
                with z.open(info) as f:data=f.read(MAX_IMAGE_BYTES+1)
                size=(240,240) if name=='main.png' else (96,74) if name=='tab.png' else (370,320)
                errors.extend(name+': '+e for e in validate_png(data,size))
                if name in expected_hashes and digest(data)!=expected_hashes[name]:errors.append(name+': Render hash mismatch')
            if not set(expected_hashes).issubset(expected):errors.append('Unknown expected render names')
    except Exception as e:errors.append(str(e))
    return {'valid':not errors,'errors':errors,'entries':entries}


def _asset_bytes(root:Path, project:dict, asset_id:str)->bytes:
    from .project import safe_path
    asset=next((a for a in project['assets'] if a['assetId']==asset_id),None)
    if asset is None:raise ValueError('Missing asset '+str(asset_id))
    path=safe_path(root,asset['relativePath'])
    if path.stat().st_size>20*1024*1024:raise ValueError('Source exceeds 20 MiB')
    data=path.read_bytes()
    if digest(data)!=asset['sha256']:raise ValueError('Asset hash mismatch: '+asset_id)
    return data


def _support_image(data:bytes,size:tuple[int,int])->bytes:
    with Image.open(io.BytesIO(data)) as im:
        if im.width*im.height>16_000_000:raise ValueError('Source exceeds pixel limit')
        im=im.convert('RGBA');bbox=im.getchannel('A').getbbox()
        if not bbox:raise ValueError('Support image base is empty')
        art=im.crop(bbox);art.thumbnail((size[0]-20,size[1]-20),Image.Resampling.LANCZOS)
        target=Image.new('RGBA',size);target.alpha_composite(art,((size[0]-art.width)//2,(size[1]-art.height)//2))
        out=io.BytesIO();target.save(out,format='PNG',dpi=(72,72));return out.getvalue()


def package_images(project_dir:Path)->tuple[dict,dict[str,bytes]]:
    from .project import load_project
    p=load_project(project_dir);slots=sorted(p['slots'],key=lambda s:s['ordinal'])
    if len(slots) not in COUNTS or len(slots)!=p['target']['count']:raise ValueError('Unsupported sticker count')
    if [s['ordinal'] for s in slots]!=list(range(1,len(slots)+1)):raise ValueError('Slot ordinals must be contiguous')
    images={}
    for s in slots:
        if s.get('renderFingerprint')!=render_fingerprint(p,s):raise ValueError('Stale render for slot '+str(s['ordinal']))
        recipe=next(r for r in p['recipes'] if r['recipeId']==s['recipeId'])
        _asset_bytes(project_dir,p,recipe['sourceAssetId'])
        data=_asset_bytes(project_dir,p,s.get('renderAssetId'))
        review=next((r for r in reversed(p['reviews']) if r.get('slotId')==s['slotId']),None)
        if not review or review.get('renderHash')!=digest(data) or review.get('visualStatus')!='pass':raise ValueError('Missing current visual approval for slot '+str(s['ordinal']))
        errors=validate_png(data,(370,320))
        if errors:raise ValueError(f"Slot {s['ordinal']}: "+'; '.join(errors))
        images[f"{s['ordinal']:02d}.png"]=data
    for name,size,key in [('main.png',(240,240),'mainSlotId'),('tab.png',(96,74),'tabSlotId')]:
        sid=p.get(key);slot=next((s for s in slots if s['slotId']==sid),slots[0])
        images[name]=_support_image(_asset_bytes(project_dir,p,slot.get('baseAssetId')),size)
    return p,images


def _contact_sheet(images:dict[str,bytes],count:int)->bytes:
    cols=4;cellw=205;cellh=205;rows=(count+cols-1)//cols
    sheet=Image.new('RGB',(cols*cellw,rows*cellh),'#e5e7eb');draw=ImageDraw.Draw(sheet)
    for i in range(count):
        x=(i%cols)*cellw;y=(i//cols)*cellh
        draw.rounded_rectangle((x+5,y+5,x+cellw-5,y+cellh-5),radius=8,fill='white')
        im=Image.open(io.BytesIO(images[f'{i+1:02d}.png'])).convert('RGBA');im.thumbnail((185,170))
        sheet.paste(im,(x+(cellw-im.width)//2,y+12),im)
        draw.text((x+12,y+185),f'{i+1:02d}',fill='#111827')
    out=io.BytesIO();sheet.save(out,format='PNG',dpi=(72,72));return out.getvalue()


def preview_sheet(project_dir:Path, target:Path)->dict:
    """Create a technical preview without asserting human visual approval."""
    from .project import load_project, safe_path
    project_dir, target = Path(project_dir), Path(target)
    p=load_project(project_dir);images={}
    resolved_target = target.resolve()
    protected = [safe_path(project_dir, 'project.json')]
    protected.extend(safe_path(project_dir, asset['relativePath']) for asset in p['assets'])
    if target.is_symlink() or any(resolved_target == path.resolve() for path in protected):
        raise ValueError('Preview cannot replace the manifest, an asset, or a symbolic link')
    if resolved_target.is_relative_to(project_dir.resolve()) and target.exists():
        raise ValueError('Preview cannot replace an existing project file; choose a new output path')
    for slot in p['slots']:
        if slot.get('renderFingerprint')!=render_fingerprint(p,slot):raise ValueError('Render is stale')
        data=_asset_bytes(project_dir,p,slot.get('renderAssetId'))
        errors=validate_png(data,(370,320))
        if errors:raise ValueError('; '.join(errors))
        images[f"{slot['ordinal']:02d}.png"]=data
    data=_contact_sheet(images,len(p['slots']));target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    pending=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
            pending=Path(f.name);f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(pending,target)
    finally:
        if pending and pending.exists():pending.unlink()
    return {'contact_sheet':str(target),'visual_review':'pending','project_revision':p['revision']}


def build_pack(project_dir:Path,output_dir:Path)->dict:
    from .backup import export_backup
    project_dir=Path(project_dir);output_dir=Path(output_dir)
    p,images=package_images(project_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.building-',dir=output_dir))
    try:
        archive=staging/'line-upload.zip'
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            for name in sorted(images):
                info=zipfile.ZipInfo(name,(2020,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
                z.writestr(info,images[name])
        hashes={name:digest(data) for name,data in images.items()}
        report=validate_pack(archive,len(p['slots']),hashes)
        if not report['valid']:raise ValueError('; '.join(report['errors']))
        (staging/'contact-sheet.png').write_bytes(_contact_sheet(images,len(p['slots'])))
        export_backup(project_dir,staging/'project-backup.zip')
        zip_hash=digest(archive.read_bytes())
        final=output_dir/f"r{p['revision']}-{zip_hash[:12]}-{uuid.uuid4().hex[:8]}"
        os.replace(staging,final)
        return {'upload_zip':str(final/'line-upload.zip'),'contact_sheet':str(final/'contact-sheet.png'),'backup_zip':str(final/'project-backup.zip'),'project_revision':p['revision'],'zip_hash':zip_hash,'validation':report}
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True);raise
