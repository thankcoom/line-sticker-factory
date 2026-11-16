#!/usr/bin/env python3
"""Local, provider-neutral sticker workflow. Image generation stays in the agent."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from sticker_factory.project import (create_project, load_project, save_project, register_asset,
    record_result, revise_caption, safe_path, now, canonical_json)
from sticker_factory.package import build_pack, package_images, render_fingerprint, validate_pack, validate_png, digest, preview_sheet


def advance(p):
    p['parentRevision']=p['revision'];p['revision']+=1;p['updatedAt']=now();p.pop('manifestHash',None)
    for package in p['packages']:
        package['status']='stale'
        package.setdefault('validation',{})['valid']=False


def get_slot(p,ordinal):
    slot=next((s for s in p['slots'] if s['ordinal']==ordinal),None)
    if slot is None:raise ValueError('Slot number is outside this project')
    return slot


def add_asset(p,asset):
    old=next((a for a in p['assets'] if a['sha256']==asset['sha256']),None)
    if old:return old
    p['assets'].append(asset);return asset


def parse_crop(value):
    try:
        values=[int(v) for v in value.split(',')]
        if len(values)!=4 or min(values[:2])<0 or min(values[2:])<=0:raise ValueError()
        return dict(zip(('x','y','width','height'),values))
    except Exception:raise argparse.ArgumentTypeError('Crop must be x,y,width,height with nonnegative origin and positive size')


def run(args):
    root=Path(args.project)
    if args.command=='init':
        if (root/'project.json').exists():raise ValueError('Project already exists; use status or choose another directory')
        p=create_project(args.count,{'name':args.name,'language':'zh-Hant'});save_project(root,p)
        return {'project_dir':str(root),'projectId':p['projectId'],'count':args.count}
    if args.command=='restore':
        from sticker_factory.backup import restore_backup
        return restore_backup(Path(args.archive),root)
    if args.command=='migrate':
        from sticker_factory.legacy import migrate_legacy
        path=Path(args.file)
        if path.stat().st_size>500*1024*1024:raise ValueError('Legacy JSON exceeds 500 MiB')
        return migrate_legacy(json.loads(path.read_text(encoding='utf-8')),root)
    p=load_project(root)
    if args.command=='status':
        return {'projectId':p['projectId'],'revision':p['revision'],'count':len(p['slots']),
                'slots':[{'ordinal':s['ordinal'],'caption':s['caption'],'status':s['status'],'has_source':bool(s.get('sourceAssetId')),'has_render':bool(s.get('renderAssetId'))} for s in p['slots']]}
    if args.command=='import':
        s=get_slot(p,args.slot);asset=register_asset(root,Path(args.image),'source')
        params={'crop':args.crop,'background':{'mode':'edge-color','color':args.background,'tolerance':args.tolerance} if args.background else {'mode':'preserve-alpha'}}
        image_contains_caption=getattr(args,'image_contains_caption',False)
        if type(image_contains_caption) is not bool:raise ValueError('imageContainsCaption must be true or false')
        expected_caption=getattr(args,'expected_caption',None)
        if expected_caption is not None and (not isinstance(expected_caption,str) or len(expected_caption)>1000 or not image_contains_caption):raise ValueError('Expected caption requires an integrated image and at most 1000 characters')
        attempt_id=args.attempt or 'import-'+digest(canonical_json({'slot':s['slotId'],'source':asset['sha256'],'params':params,'imageContainsCaption':image_contains_caption,**({'expectedCaption':expected_caption} if expected_caption is not None else {})}))[:24]
        if not args.attempt and (s.get('selectedAttemptId') or '').startswith(attempt_id+'-r'):
            attempt_id=s['selectedAttemptId']
        existing=next((a for a in p['attempts'] if a['attemptId']==attempt_id),None)
        if existing and not args.attempt and s.get('selectedAttemptId')!=attempt_id:
            # Re-selecting an earlier source/crop is a new edit, not a replay
            # of the old import's result.
            attempt_id+='-r'+str(p['revision']);existing=None
        if existing:
            if existing.get('slotIds')!=[s['slotId']]:raise ValueError('Attempt belongs to another slot')
            if bool(existing.get('imageContainsCaption'))!=image_contains_caption:raise ValueError('Attempt has a different text declaration; create a new attempt')
            if existing.get('expectedCaption')!=expected_caption:raise ValueError('Attempt has a different expected caption; create a new attempt')
            result=record_result(p,attempt_id,asset)
            if result==p:return {'status':'noop','slot':args.slot,'revision':p['revision']}
        else:
            p['attempts'].append({'attemptId':attempt_id,'jobId':attempt_id,'slotIds':[s['slotId']],'planRevision':s['planRevision'],'executor':'file-import','prompt':'','referenceAssetIds':[],'status':'awaiting_asset','sourceAssetIds':[],'error':None,'imageContainsCaption':image_contains_caption,**({'expectedCaption':expected_caption} if expected_caption is not None else {})})
            result=record_result(p,attempt_id,asset)
        s=get_slot(result,args.slot)
        recipe={'recipeId':'recipe-'+s['slotId'],'sourceAssetId':s['sourceAssetId'],'revision':result['revision'],'captionLayer':{'text':s['caption'],**({'enabled':False} if s.get('captionLocked') or result['brief'].get('textStrategy')=='none' else {})},'target':{'width':370,'height':320,'margin':10},**params}
        result['recipes']=[r for r in result['recipes'] if r['recipeId']!=recipe['recipeId']]+[recipe]
        s['recipeId']=recipe['recipeId'];s.pop('renderFingerprint',None)
        save_project(root,result);return {'status':'imported','slot':args.slot,'revision':result['revision']}
    if args.command=='caption':
        s=get_slot(p,args.slot);p=revise_caption(p,s['slotId'],args.text);save_project(root,p)
        return {'status':'saved','revision':p['revision']}
    if args.command=='plan':
        if Path(args.file).stat().st_size>1024*1024:raise ValueError('Plan is too large')
        document=json.loads(Path(args.file).read_text())
        entries=document.get('slots') if isinstance(document,dict) else document
        if isinstance(document,dict):
            if document.get('schemaVersion')!=1 or not isinstance(document.get('brief',{}),dict):raise ValueError('Unsupported plan schema')
            if document.get('brief',{}).get('count',p['target']['count'])!=p['target']['count']:raise ValueError('Plan count does not match project')
            updated_brief={**p['brief'],**document.get('brief',{})}
            if updated_brief!=p['brief']:p['brief']=updated_brief;advance(p)
        if not isinstance(entries,list) or len(entries)!=len(p['slots']) or sorted(e.get('ordinal') for e in entries)!=list(range(1,len(entries)+1)):raise ValueError('Plan must contain every ordinal exactly once')
        for entry in entries:
            if not all(isinstance(entry.get(k),str) for k in ('caption','action','useCase')):raise ValueError('Each plan item needs caption, action and useCase strings')
            s=get_slot(p,entry['ordinal']);p=revise_caption(p,s['slotId'],entry['caption']);s=get_slot(p,entry['ordinal'])
            if s['action']!=entry['action'] or s['useCase']!=entry['useCase']:
                s.update(action=entry['action'],useCase=entry['useCase'],planRevision=s['planRevision']+1,status='stale' if s.get('renderAssetId') else 'planned')
                p['reviews']=[review for review in p['reviews'] if review.get('slotId')!=s['slotId']]
                advance(p)
        save_project(root,p);return {'status':'planned','revision':p['revision']}
    if args.command=='render':
        from sticker_factory.render import render_slot
        slots=[get_slot(p,args.slot)] if args.slot else p['slots']
        done=[]
        for s in slots:
            recipe=next((r for r in p['recipes'] if r['recipeId']==s.get('recipeId')),None)
            if recipe is None:raise ValueError(f"Slot {s['ordinal']} has no imported source")
            source=next(a for a in p['assets'] if a['assetId']==recipe['sourceAssetId'])
            path=safe_path(root,source['relativePath'])
            if digest(path.read_bytes())!=source['sha256']:raise ValueError('Source hash mismatch')
            recipe.setdefault('captionLayer',{})['text']=s['caption']
            if s.get('captionLocked'):recipe['captionLayer']['enabled']=False
            result=render_slot(path,recipe,Path(args.font) if args.font else None,args.font_index)
            recipe['fontHash']=result['font_hash'];recipe['fontIndex']=args.font_index
            with tempfile.TemporaryDirectory(dir=root,prefix='.render-') as tmp:
                for key,kind,slotkey in [('base_png','base','baseAssetId'),('render_png','render','renderAssetId')]:
                    target=Path(tmp)/(kind+'.png');target.write_bytes(result[key]);asset=add_asset(p,register_asset(root,target,kind));s[slotkey]=asset['assetId']
            new_fingerprint=render_fingerprint(p,s)
            if s.get('renderFingerprint')!=new_fingerprint or s.get('renderHash')!=result['render_hash']:
                p['reviews']=[r for r in p['reviews'] if r.get('slotId')!=s['slotId']]
            s.update(renderFingerprint=new_fingerprint,renderHash=result['render_hash'],status='processed')
            advance(p);save_project(root,p);done.append(s['ordinal'])
        return {'status':'rendered','slots':done,'revision':p['revision'],'note':'Visual review is still required before packaging'}
    if args.command=='review':
        s=get_slot(p,args.slot)
        if s.get('renderFingerprint')!=render_fingerprint(p,s):raise ValueError('Render is stale; render before reviewing')
        asset=next((a for a in p['assets'] if a['assetId']==s.get('renderAssetId')),None)
        if asset is None:raise ValueError('No rendered image')
        data=safe_path(root,asset['relativePath']).read_bytes();errors=validate_png(data,(370,320))
        if errors or digest(data)!=asset['sha256']:raise ValueError('Rendered file validation failed: '+'; '.join(errors))
        p['reviews']=[r for r in p['reviews'] if r.get('slotId')!=s['slotId']]
        p['reviews'].append({'slotId':s['slotId'],'renderHash':digest(data),'technicalResults':[],'visualStatus':args.status,'reviewer':args.reviewer,'reasons':[args.reason] if args.reason else [],'checkedAt':now()})
        s['status']='reviewed' if args.status=='pass' else 'processed';advance(p);save_project(root,p)
        return {'status':args.status,'slot':args.slot,'revision':p['revision']}
    if args.command=='preview':return preview_sheet(root,Path(args.out))
    if args.command=='build':return build_pack(root,Path(args.out))
    if args.command=='validate':
        project,images=package_images(root)
        result=validate_pack(Path(args.zip),len(project['slots']),{name:digest(data) for name,data in images.items()})
        if not result['valid']:raise ValueError('; '.join(result['errors']))
        return result
    raise ValueError('Unknown command')


def parser():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    for command in ('init','import','plan','caption','render','review','build','validate','status','restore','preview','migrate'):
        s=sub.add_parser(command);s.add_argument('--project',required=True)
        if command=='init':s.add_argument('--count',type=int,choices=(8,16,24,32,40),default=8);s.add_argument('--name',default='LINE 貼圖專案')
        if command in ('import','caption','review'):s.add_argument('--slot',type=int,required=True)
        if command=='import':
            s.add_argument('--image',required=True);s.add_argument('--attempt');s.add_argument('--crop',type=parse_crop);s.add_argument('--background');s.add_argument('--tolerance',type=int,default=0)
            s.add_argument('--image-contains-caption',action='store_true',help='Source lettering is already integrated; disable the editable caption layer')
            s.add_argument('--expected-caption',help='Expected wording in a new integrated source; cannot relabel known source bytes')
        if command=='caption':s.add_argument('--text',required=True)
        if command in ('plan','migrate'):s.add_argument('--file',required=True)
        if command=='render':s.add_argument('--slot',type=int);s.add_argument('--font');s.add_argument('--font-index',type=int,default=0)
        if command=='review':s.add_argument('--status',choices=('pass','rework'),required=True);s.add_argument('--reviewer',required=True);s.add_argument('--reason',default='')
        if command in ('build','preview'):s.add_argument('--out',required=True)
        if command=='validate':s.add_argument('--zip',required=True)
        if command=='restore':s.add_argument('--archive',required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        result=run(args);print(json.dumps(result,ensure_ascii=False));return 0
    except (ValueError,OSError,KeyError,TypeError,StopIteration) as e:
        print(json.dumps({'error':str(e)},ensure_ascii=False),file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
