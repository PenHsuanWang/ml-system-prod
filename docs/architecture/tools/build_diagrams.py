"""Render the seven implementation views and index the hand-authored context SVG."""
from pathlib import Path
import json
import math
import re
import textwrap
import xml.etree.ElementTree as ET

import application_views
import deployment_views
import sequence_views

DOC = Path(__file__).resolve().parents[1]
BASELINE = '8c9023b00828000287e169ac760afe57102d7353'
NS = 'http://www.w3.org/2000/svg'
INK, SUB, RULE = '#223b40', '#41585c', '#bdc9c8'
# Fill, outline and human-readable boundary key.
PALETTE = {
    'participant': ('#e8efed', '#77918e', 'Lifeline participant'),
    'system': ('#eff5f3', '#688983', 'Product'),
    'group': ('#f8f9f7', '#afb9b0', 'Functional group'),
    'logical': ('#e8eeea', '#748b7a', 'Logical component'),
    'process': ('#e3edf1', '#6e909b', 'OS process / role'),
    'host': ('#f7f7f3', '#8e9384', 'Host'),
    'container': ('#eaf0e3', '#869274', 'Docker / OCI container'),
    'pod': ('#f0eaf1', '#9c849d', 'Kubernetes Pod'),
    'resource': ('#eaf1ee', '#799187', 'Kubernetes API resource'),
    'workload': ('#f4f6ee', '#899777', 'Kubernetes Deployment'),
    'namespace': ('#f2f4f9', '#8e9bae', 'Kubernetes namespace'),
    'network': ('#f2f4f9', '#8e9bae', 'Network / namespace'),
    'external': ('#f4ebe0', '#a18c70', 'External system'),
    'storage': ('#e6edeb', '#78918c', 'Store / volume'),
}
# Colour, dash pattern and meaning. 'return' is a response on the current call.
STYLES = {
    'sync': ('#3f6574', '', 'Call'),
    'return': ('#667c84', '5 4', 'Response'),
    'async': ('#477366', '8 4', 'Message'),
    'data': ('#706247', '3 3', 'State / artifact I/O'),
    'manual': ('#915e40', '10 3 2 3', 'Operator request'),
    'observe': ('#795e79', '7 3 2 3', 'Observe / reconcile'),
    'launch': ('#386c67', '', 'Launch'),
    'dependency': ('#6b6785', '2 4', 'Declared dependency'),
    'wire': ('#986f40', '10 4', 'Startup wiring'),
    'branch': ('#985953', '5 3', 'Conditional exit / path'),
}


def elem(parent, tag, **attrs):
    return ET.SubElement(parent, tag, {k.replace('_', '-'): str(v) for k, v in attrs.items()})


def label(parent, x, y, value, size, *, bold=False, anchor='start', color=INK, **attrs):
    elem(parent, 'text', x=x, y=y, font_size=size, font_weight='bold' if bold else 'normal',
         text_anchor=anchor, fill=color, **attrs).text = value


def rect(parent, x, y, w, h, **attrs):
    return elem(parent, 'rect', x=x, y=y, width=w, height=h, **attrs)


def segments(points):
    return zip(points, points[1:])


def overlaps(a, b, gap=0):
    x,y,w,h = a
    X,Y,W,H = b
    return x < X+W+gap and x+w+gap > X and y < Y+H+gap and y+h+gap > Y


def evidence(value):
    if isinstance(value, str):
        return re.findall(r'E\d+', value)
    return list(value)


def context_model():
    file = '01-system-context.svg'
    root = ET.parse(DOC/'diagrams'/file).getroot()
    ns = {'s': NS}
    nodes, groups, relations = [], [], []
    for group in root.findall('.//s:g[@id]', ns):
        box = group.find('s:rect', ns)
        if box is None:
            continue
        title = group.find('s:text', ns).text
        item = dict(id=group.get('id'), title=title, type=group.get('data-kind','system'),
                    parent='product' if group.get('id')=='system' else None)
        item.update({key:int(box.get(attr)) for key,attr in zip(('x','y','w','h'),('x','y','width','height'))})
        (groups if group.get('data-boundary') else nodes).append(item)
    for group in root.iter(f'{{{NS}}}g'):
        children = list(group)
        for idx, path in enumerate(children):
            if path.get('data-source') is None:
                continue
            coords = list(map(int,re.findall(r'-?\d+',path.get('d'))))
            relations.append(dict(id=path.get('id'),source=path.get('data-source'),target=path.get('data-target'),
                                  points=list(zip(coords[::2],coords[1::2])),label=children[idx+1].text,
                                  kind=path.get('marker-end')[11:-1],evidence=path.get('data-evidence').split(',')))
    return dict(file=file,kind='context',title='System context',width=int(root.get('width')),
                height=int(root.get('height')),groups=groups,nodes=nodes,relationships=relations)


class Canvas:
    def __init__(self, spec, width):
        self.spec = spec
        self.width = width
        self.root = ET.Element('svg', xmlns=NS, role='img', attrib={'aria-labelledby':'svg-title svg-desc'})
        elem(self.root,'title',id='svg-title').text = spec['title']
        elem(self.root,'desc',id='svg-desc').text = spec['subtitle']+' | '+BASELINE
        defs = elem(self.root,'defs')
        for kind,(color,_,_) in STYLES.items():
            marker = elem(defs,'marker',id='arrow-'+kind,viewBox='0 0 8 8',refX=8,refY=4,
                          markerWidth=7,markerHeight=7,orient='auto',markerUnits='userSpaceOnUse')
            elem(marker,'path',d='M 0 0 L 8 4 L 0 8 Z',fill=color)
        rect(self.root,0,0,'100%','100%',fill='white')
        self.body = elem(self.root,'g',font_family='Arial, Helvetica, sans-serif')
        number = spec['id'][:2]
        label(self.body,32,26,'ML SYSTEM EBM   /   ARCHITECTURE GUIDE',12,bold=True,color=SUB)
        label(self.body,width-32,26,number+'   /   '+BASELINE[:8],12,anchor='end',color=SUB)
        title = re.sub(r'^\d+\s*[|·–—-]\s*','',spec['title'])
        label(self.body,32,61,title,28,bold=True)
        label(self.body,32,88,spec['subtitle'],15,color=SUB)
        self.model = dict(file=spec['id']+'.svg',kind=spec['kind'],title=title,subtitle=spec['subtitle'],
                          width=width,groups=[],nodes=[],relationships=[],notes=spec['notes'])

    def edge(self, rel):
        kind,points = rel['kind'],rel['points']
        color,dash,_ = STYLES[kind]
        path = 'M '+' L '.join(f'{x} {y}' for x,y in points)
        # White casing preserves continuity at orthogonal crossings.
        elem(self.body,'path',d=path,fill='none',stroke='white',stroke_width=4.5)
        p = elem(self.body,'path',id=rel['id'],d=path,fill='none',stroke=color,stroke_width=1.8,
                 marker_end=f'url(#arrow-{kind})',data_source=rel['source'],data_target=rel['target'],
                 data_kind=kind,data_evidence=','.join(rel['evidence']))
        if dash:
            p.set('stroke-dasharray',dash)
        elem(p,'title').text = rel['label']
        self.model['relationships'].append(rel)

    def finish(self,y):
        body,width=self.body,self.width
        elem(body,'path',d=f'M 32 {y} H {width-32}',stroke=RULE)
        y+=28
        x=32
        for kind in dict.fromkeys(r['kind'] for r in self.model['relationships']):
            color,dash,caption=STYLES[kind]
            advance=56+8*len(caption)
            if x+advance>width-32:
                x=32
                y+=25
            elem(body,'path',d=f'M {x} {y-4} h 25',stroke=color,stroke_width=2,
                 stroke_dasharray=dash or 'none',marker_end=f'url(#arrow-{kind})')
            label(body,x+32,y,caption,13,color=SUB)
            x+=advance
        if self.spec['kind']=='topology':
            x=32
            y+=28
            for kind in dict.fromkeys(b['type'] for b in self.model['groups']+self.model['nodes']):
                fill,color,caption=PALETTE[kind]
                advance=33+8*len(caption)
                if x+advance>width-32:
                    x=32
                    y+=25
                rect(body,x,y-11,13,13,fill=fill,stroke=color)
                label(body,x+20,y,caption,13,color=SUB)
                x+=advance
        for note in self.spec['notes']:
            y+=23
            for i,line in enumerate(textwrap.wrap(note,width=max(40,int((width-78)/7.8)))):
                if i:
                    y+=18
                label(body,32,y,line,14,color=SUB)
        height=math.ceil(y+30)
        self.root.attrib.update(width=str(width),height=str(height),viewBox=f'0 0 {width} {height}')
        ET.indent(self.root)
        (DOC/'diagrams'/self.model['file']).write_text(ET.tostring(self.root,encoding='unicode')+'\n')
        self.model['height']=height
        return self.model


def graph_box(canvas, item, group):
    id_,parent,x,y,w,h,title,sub,kind=item
    box=dict(zip(('id','parent','x','y','w','h','title','subtitle','type'),item))
    canvas.model['groups' if group else 'nodes'].append(box)
    fill,stroke,_=PALETTE[kind]
    g=elem(canvas.body,'g',id=id_,data_parent=parent or '',data_kind=kind)
    elem(g,'title').text=title+' | '+sub
    rect(g,x,y,w,h,rx=4,fill=fill,stroke=stroke,stroke_width=1.2,
         stroke_dasharray='6 4' if group else 'none')
    if group:
        label(g,x+12,y+20,title,15,bold=True)
        for i,line in enumerate(sub.splitlines()):
            label(g,x+12,y+37+i*14,line,12,color=SUB)
    else:
        titles,subs=title.splitlines(),sub.splitlines()
        top=y+(h-(len(titles)*17+len(subs)*15))/2+13
        for i,line in enumerate(titles):
            label(g,x+w/2,top+17*i,line,14,bold=True,anchor='middle')
        for i,line in enumerate(subs):
            label(g,x+w/2,top+17*len(titles)+15*i,line,12,anchor='middle',color=SUB)


# Direct labels keep the graph readable at normal zoom. Longer descriptions and
# the corresponding evidence remain available in the SVG title and evidence map.
SHORT_LABELS = {
    'ingress-mgmt':'HTTP', 'identity':'Session', 'app-sql':'SQL', 'migrate-sql':'SQL',
    'poll-ingestion':'Claim ingestion work', 'receive':'AMQP', 'start-guardian':'Fork',
    'start-runner':'Spawn', 'gateway-ingress':'HTTP', 'serve-route':'HTTP', 'ensure-svc':'Ensure Service',
    'ensure-spec':'Apply', 'read-spec':'Read spec', 'cache-fill':'Cache', 'observe-evidence':'Read evidence',
    'model-read':'Fetch pinned model', 'tls-upstream':'HTTP', 'artifact-store':'S3',
    'create-bucket':'Initialize', 'ensure-deployment':'Ensure Deployment', 'runtime-files':'Attempt files',
    'identity-management':'Auth', 'identity-gateway':'Auth', 'identity-serving':'Revalidate bearer',
    'serve-request':'HTTP', 'ensure-serving':'Reconcile', 'admission':'Admit / govern',
    'route':'Read route', 'reconcile':'Fence / activate', 'ingest-state':'Claim / commit',
    'lease':'Lease outbox', 'publish':'Publish', 'delivery':'Deliver', 'ack':'ACK',
    'attempt':'Claim attempt', 'fit':'Launch fit', 'raw':'Publish snapshot', 'fetch':'Read source',
    'fit-files':'Read dataset / write result', 'ack-snapshot':'Save snapshot',
    'register':'Register bundle', 'materialize':'Fetch pinned bundle', 'track-metadata':'SQL',
    'track-bytes':'S3', 'wire-router':'Mount', 'wire-service':'Inject adapters', 'actor':'Resolve',
    'authenticate':'Session', 'submit':'submit()', 'domain-imports':'Import',
    'resolve':'resolve()', 'dataset-version':'Resolve version', 'catalog-read':'Read metadata',
    'persist':'submit()', 'imports':'Type imports', 'transaction':'Commit',
    'demo-launch':'Spawn', 'demo-output':'Write bundle', 'demo-persist':'Lease job / store evidence',
    'predict':'HTTP', 'serve-launch':'Spawn', 'admit':'Commit', 'consume':'Deliver', 'snapshot':'Persist',
    'train-launch':'Spawn', 'train-files':'Attempt output', 'publish-raw':'Publish', 'schema':'Migrate',
    'management-sql':'SQL', 'management-auth':'Session lookup', 'migration':'Migrate',
    'ingest-start':'After migration succeeds', 'fork':'Spawn', 'result':'Save terminal snapshot',
    'registration':'Register bundle', 'serving-spawn':'Start', 'forward':'HTTP',
    'mlflow-db':'PostgreSQL', 'mlflow-s3':'S3 artifact proxy', 'make-bucket':'Create bucket',
}


def tag_position(rel, obstacles, previous, others, width):
    for a,b in sorted(segments(rel['points']),key=lambda p:-math.dist(*p)):
        length=int(math.dist(a,b))
        if length<12:
            continue
        for step in sorted(range(6,length-5),key=lambda d:abs(d-length/2)):
            x=round(a[0]+(b[0]-a[0])*step/length)
            y=round(a[1]+(b[1]-a[1])*step/length)
            # Beside the routed line. Neither the label nor its backing covers the arrow.
            if a[1]==b[1]:
                candidates=[(x-width/2,y-22),(x-width/2,y+4)]
            else:
                candidates=[(x+4,y-9),(x-width-4,y-9)]
            for left,top in candidates:
                box=(round(left),round(top),width,16)
                if not any(overlaps(box,obs,1) for obs in obstacles+previous+others):
                    return box
    return None


def graph(spec):
    c=Canvas(spec,spec['width'])
    for item in spec['groups']:
        graph_box(c,item,True)
    for item in spec['nodes']:
        graph_box(c,item,False)
    for i,(id_,source,target,points,kind,label_,refs) in enumerate(spec['edges'],1):
        c.edge(dict(id=id_,source=source,target=target,points=points,kind=kind,label=label_,
                    evidence=evidence(refs),key=i))
    obstacles=[(n['x'],n['y'],n['w'],n['h']) for n in c.model['nodes']]
    for group in c.model['groups']:
        # Only the actual caption block is occupied; space to its right can route a line.
        letters=max(len(group['title'])*8,len(group['subtitle'])*6)
        obstacles.append((group['x']+7,group['y']+3,min(group['w']-12,letters+8),39))
    obstacles.extend([(-2000,-2000,2012,10000),(c.width-12,-2000,2012,10000)])
    rels=c.model['relationships']
    occupied=[]
    keyed=[]
    for rel in rels:
        lines=[(min(x,X)-1,min(y,Y)-1,abs(x-X)+2,abs(y-Y)+2)
               for other in rels for (x,y),(X,Y) in segments(other['points'])]
        caption=SHORT_LABELS.get(rel['id'],rel['id'].replace('-',' ').capitalize())
        width=math.ceil(len(caption)*7+8)
        box=tag_position(rel,obstacles,occupied,lines,width)
        if box is None:
            caption=str(rel['key'])
            box=tag_position(rel,obstacles,occupied,lines,18)
            keyed.append(rel)
        if box is None:
            raise ValueError(f"No clear position for {rel['id']}")
        occupied.append(box)
        x,y,w,h=box
        color=STYLES[rel['kind']][0]
        rect(c.body,x,y,w,h,rx=2,fill='white',fill_opacity=.96)
        label(c.body,x+w/2,y+12,caption,12,anchor='middle',color=color)
        rel['caption']=caption
    bottom=max([n['y']+n['h'] for n in c.model['nodes']+c.model['groups']]+
               [y for rel in rels for x,y in rel['points']]+[y+h for x,y,w,h in occupied])
    y=bottom+24
    for rel in keyed:
        label(c.body,32,y,f"{rel['key']}   {rel['label']}",14)
        y+=23
    return c.finish(y)


def sequence(spec):
    people=spec['participants']
    pitch=115 if len(people)>10 else 151
    width=max(1050,80+pitch*len(people))
    positions={p[0]:round((i+.5)*(width-64)/len(people))+32 for i,p in enumerate(people)}
    c=Canvas(spec,width)
    phase_y=197
    phases=[]
    for name,messages in spec['phases']:
        phase={'title':name,'y':phase_y,'messages':[]}
        cursor=phase_y+56
        for src,dst,msg,kind,refs in messages:
            cursor+=34
            x,X=positions[src],positions[dst]
            if src==dst:
                side=1 if x<width/2 else -1
                points=[(x,cursor),(x+30*side,cursor),(x+30*side,cursor+13),(x,cursor+13)]
                cursor+=13
            else:
                points=[(x,cursor),(X,cursor)]
            phase['messages'].append(dict(id=f"msg-{sum(len(p['messages']) for p in phases)+len(phase['messages'])+1:02}",
                                          source=src,target=dst,points=points,label=msg,kind=kind,evidence=evidence(refs)))
        phases.append(phase)
        phase_y=cursor+22
    c.model['phases']=[{'title':p['title'],'relationships':[m['id'] for m in p['messages']]} for p in phases]
    for id_,title,sub in people:
        x=positions[id_]
        w=min(142,(width-64)//len(people)-8)
        graph_box(c,(id_,None,x-w//2,114,w,61,title,sub,'participant'),False)
        c.model['nodes'][-1].update(axis=x,lifelineEnd=phase_y)
        elem(c.body,'path',id='life-'+id_,d=f'M {x} 175 V {phase_y}',stroke='#a5b3b2',
             stroke_width=1,stroke_dasharray='3 4')
    for i,phase in enumerate(phases):
        y=phase['y']
        rect(c.body,24,y,width-48,27,rx=3,fill='#e7efed')
        label(c.body,34,y+19,phase['title'],15,bold=True)
        for id_,title,_ in people:
            label(c.body,positions[id_],y+46,title.replace('\n',' '),12,anchor='middle',color=SUB,
                  stroke='white',stroke_width=4,paint_order='stroke')
        for r in phase['messages']:
            c.edge(r)
            x,y=r['points'][0]
            X=r['points'][-1][0]
            mid=(x+X)/2
            # Keep the whole message on canvas even when the sender calls itself at an edge.
            reserve=min(width/2-32, max(40,len(r['label'])*4.2))
            mid=min(max(mid,reserve+20),width-reserve-20)
            label(c.body,mid,y-7,r['label'],14,anchor='middle',color=STYLES[r['kind']][0],
                  stroke='white',stroke_width=4,paint_order='stroke',stroke_linejoin='round')
    return c.finish(phase_y+12)


if __name__=='__main__':
    diagrams=[context_model()]
    for view in application_views.views()+sequence_views.views()+deployment_views.views():
        diagrams.append(sequence(view) if view['kind']=='sequence' else graph(view))
    diagrams.sort(key=lambda d:d['file'])
    (DOC/'diagram-model.json').write_text(json.dumps(dict(source_commit=BASELINE,diagrams=diagrams),
                                                     indent=2,ensure_ascii=False)+'\n')
    print('Indexed context and rendered',len(diagrams)-1,'implementation views')
