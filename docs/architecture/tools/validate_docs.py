"""Read-only documentation checks. Does not import the app or start infrastructure."""
from pathlib import Path
import json
import re
import subprocess
import xml.etree.ElementTree as ET

D=Path(__file__).resolve().parents[1]; R=D.parents[1]
NS='{http://www.w3.org/2000/svg}'
model=json.loads((D/'diagram-model.json').read_text())
evidence=json.loads((D/'evidence-index.json').read_text())
errors=[];warnings=[];counts={'svg':0,'source_references':0,'local_links':0,'relationships':0}
def require(ok,message):
    if not ok:errors.append(message)
font_path=Path('/System/Library/Fonts/Supplemental/Arial.ttf')
try:
    from PIL import ImageFont
    font_available=font_path.exists()
except ImportError:
    font_available=False
if not font_available:warnings.append('Text metrics unavailable; renderer / visual inspection still required')
def geometry(box):
    return box['x'],box['y'],box['w'],box['h']


def overlap(a,b):
    x,y,w,h=a
    X,Y,W,H=b
    return x<X+W and X<x+w and y<Y+H and Y<y+h


def on_border(pt,b):
    x,y=pt
    X,Y,w,h=geometry(b)
    return ((x in (X,X+w) and Y<=y<=Y+h) or (y in (Y,Y+h) and X<=x<=X+w))


def through(a,b,box):
    x,y=a
    X,Y=b
    bx,by,w,h=box
    if y==Y:
        return by<y<by+h and min(x,X)<bx+w and max(x,X)>bx
    return bx<x<bx+w and min(y,Y)<by+h and max(y,Y)>by


def check_ref(ref):
    lines=(R/ref['path']).read_text().splitlines()
    require(0<ref['line']<=len(lines) and ref['needle'] in lines[ref['line']-1],f'Stale evidence {ref}')


counts.update(lifelines=0,containment_boundaries=0,direct_edge_sources=0)
require(model['source_commit']==evidence['source_commit'],'Model / evidence source mismatch')
for dia in model['diagrams']:
    file=D/'diagrams'/dia['file']
    require(file.is_file(),f'Missing SVG {file}')
    root=ET.parse(file).getroot()
    counts['svg']+=1
    require(root.tag==NS+'svg',f'Invalid standalone namespace {file.name}')
    require(root.get('viewBox')==f"0 0 {dia['width']} {dia['height']}",f'viewBox mismatch {file.name}')
    ids=[x.get('id') for x in root.iter() if x.get('id')]
    require(len(ids)==len(set(ids)),f'Duplicate ids {file.name}')
    elements={x.get('id'):x for x in root.iter() if x.get('id')}
    require(root.find(NS+'title') is not None and root.find(NS+'desc') is not None,f'No accessibility metadata {file.name}')
    require(not any(x.tag in {NS+'script',NS+'image',NS+'foreignObject'} for x in root.iter()),f'External content {file.name}')
    groups,boxes=dia['groups'],dia['nodes']
    family={b['id']:b for b in groups+boxes}
    counts['containment_boundaries']+=len(groups)
    for i,b in enumerate(groups+boxes):
        x,y,w,h=geometry(b)
        require(0<=x<x+w<=dia['width'] and 0<=y<y+h<=dia['height'],f'Outside canvas {file.name} {b["id"]}')
        bounds=elements[b['id']].find(NS+'rect')
        require(tuple(map(float,(bounds.get(k) for k in ('x','y','width','height'))))==(x,y,w,h),f'SVG/model box mismatch {file.name} {b["id"]}')
        if b['parent']:
            X,Y,W,H=geometry(family[b['parent']])
            require(X<x<x+w<X+W and Y<y<y+h<Y+H,f'Outside parent {file.name} {b["id"]}')
        for other in (groups+boxes)[i+1:]:
            if b['parent']==other['parent']:
                require(not overlap(geometry(b),geometry(other)),f'Overlapping siblings {file.name}: {b["id"]} / {other["id"]}')
    sequence=dia['kind']=='sequence'
    text_boxes=[]
    if font_available:
        for t in root.iter(NS+'text'):
            val=''.join(t.itertext())
            if not val:
                continue
            x,y,size=float(t.get('x')),float(t.get('y')),int(t.get('font-size'))
            fp=font_path.with_name('Arial Bold.ttf') if t.get('font-weight')=='bold' else font_path
            font=ImageFont.truetype(str(fp),size)
            l,u,r,d=font.getbbox(val,anchor='ms' if t.get('text-anchor')=='middle' else ('rs' if t.get('text-anchor')=='end' else 'ls'))
            bound=(x+l,y+u,r-l,d-u)
            require(0<=x+l<=x+r<=dia['width'] and 0<=y+u<=y+d<=dia['height'],f'Text outside canvas {file.name}: {val}')
            for prev,other in text_boxes:
                require(not overlap(bound,prev),f'Text overlap {file.name}: {val} / {other}')
            for box in boxes:
                X,Y,W,H=geometry(box)
                if overlap(geometry(box),bound):
                    require(X+3<=x+l and x+r<=X+W-3 and Y+2<=y+u and y+d<=Y+H-2,f'Text at box edge {file.name}: {val}')
            text_boxes.append((bound,val))
    if sequence:
        for b in boxes:
            axis=b['axis']
            line=elements['life-'+b['id']]
            require(line.get('d')==f'M {axis} {b["y"]+b["h"]} V {b["lifelineEnd"]}',f'Broken lifeline {file.name} {b["id"]}')
        counts['lifelines']+=len(boxes)
        require([i for p in dia['phases'] for i in p['relationships']]==[r['id'] for r in dia['relationships']],f'Phase order {file.name}')
    last_y=0
    routes=[]
    refs=evidence['relationships'][file.name]
    require(set(refs)=={r['id'] for r in dia['relationships']},f'Evidence edge set {file.name}')
    for rel in dia['relationships']:
        key=rel['id']
        counts['relationships']+=1
        ref=refs[key]
        check_ref(ref)
        counts['direct_edge_sources']+=1
        require(ref['source']==rel['source'] and ref['target']==rel['target'] and ref['groups']==rel['evidence'],f'Evidence endpoint / group mismatch {file.name} {key}')
        require(rel['evidence'] and all(e in evidence['groups'] for e in rel['evidence']),f'Unmapped relationship {file.name} {key}')
        a,b=family[rel['source']],family[rel['target']]
        pts=rel['points']
        path=elements[key]
        require(path.get('data-source')==a['id'] and path.get('data-target')==b['id'],f'Wrong SVG endpoint IDs {file.name} {key}')
        require(path.get('d')=='M '+' L '.join(f'{x} {y}' for x,y in pts),f'SVG route mismatch {file.name} {key}')
        require(path.get('marker-end')==f'url(#arrow-{rel["kind"]})',f'Marker mismatch {file.name} {key}')
        if sequence:
            require(pts[0][0]==a['axis'] and pts[-1][0]==b['axis'] and last_y<pts[0][1]<=pts[-1][1]<=b['lifelineEnd'],f'Incorrect lifeline or order {file.name} {key}')
            last_y=pts[-1][1]
        else:
            require(on_border(pts[0],a) and on_border(pts[-1],b),f'Arrow misses endpoint {file.name} {key}')
        for start,end in zip(pts,pts[1:]):
            require(start!=end and (start[0]==end[0] or start[1]==end[1]),f'Nonorthogonal or empty route {file.name} {key}')
            for box in boxes:
                require(not through(start,end,geometry(box)),f'Connector through node {file.name} {key} / {box["id"]}')
            for bound,val in text_boxes:
                require(not through(start,end,bound),f'Connector through text {file.name} {key}: {val}')
            x,y=start
            X,Y=end
            for a0,b0,other in routes:
                u,v=a0
                U,V=b0
                common=((y==Y==v==V and min(max(x,X),max(u,U))>max(min(x,X),min(u,U))) or
                        (x==X==u==U and min(max(y,Y),max(v,V))>max(min(y,Y),min(v,V))))
                require(not common,f'Collinear routes {file.name} {key} / {other}')
            routes.append((start,end,key))
for refs in evidence['groups'].values():
    for ref in refs:
        check_ref(ref)
        counts['source_references']+=1

def slug(s):
    s=s.lower().replace('`','');s=re.sub(r'[^\w\-\s\u3400-\u9fff]','',s);return re.sub(r'\s','-',s)
for md in D.glob('*.md'):
    txt=md.read_text();require(txt.count('```')%2==0,f'Unbalanced code fence {md.name}')
    for dest in re.findall(r'!?\[[^\]]*\]\(([^)]+)\)',txt):
        if re.match(r'\w+://',dest):continue
        file,_,anchor=dest.partition('#');target=(md.parent/file).resolve() if file else md
        counts['local_links']+=1;require(target.exists(),f'Broken link {md.name}: {dest}')
        if not target.exists():continue
        if anchor.startswith('L') and anchor[1:].isdigit():require(int(anchor[1:])<=len(target.read_text().splitlines()),f'Invalid source line {dest}')
        elif anchor and target.suffix=='.md':
            anchors={slug(h) for h in re.findall(r'^#+\s+(.+)$',target.read_text(),re.M)}
            require(anchor in anchors,f'Broken anchor {md.name}: {dest}')
for file in D.rglob('*'):
    if file.is_file() and file.suffix in {'.md','.py','.json','.svg'}:
        content=file.read_text()
        require(content.endswith('\n'),f'Missing final newline {file}')
        require(all(line==line.rstrip() for line in content.splitlines()),f'Trailing whitespace {file}')
guide=(D/'guidebook.md').read_text();require(len(re.findall(r'^## \d+\.',guide,re.M))==12,'Expected 12 guidebook chapters')
require(len(model['diagrams'])==8,'Expected 8 diagrams including implemented K8s')
status=subprocess.run(['git','status','--porcelain','--untracked-files=all'],cwd=R,capture_output=True,text=True,check=True).stdout
for line in status.splitlines():require(line[3:].startswith('docs/architecture/'),f'Out-of-scope change {line}')
require(subprocess.run(['git','diff','--check'],cwd=R,capture_output=True).returncode==0,'git diff --check failed')
print(json.dumps({'status':'FAIL' if errors else 'PASS','counts':counts,'warnings':warnings,'errors':errors},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
