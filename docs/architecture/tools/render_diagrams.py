"""Render SVG previews using an already installed rsvg-convert; never install tools."""
import argparse
from pathlib import Path
import shutil
import subprocess

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--output-dir',type=Path,required=True)
a=p.parse_args()
renderer=shutil.which('rsvg-convert')
if not renderer:raise SystemExit('BLOCKED: rsvg-convert is not installed')
a.output_dir.mkdir(parents=True,exist_ok=True)
files=sorted((Path(__file__).resolve().parents[1]/'diagrams').glob('*.svg'))
for src in files:
    dest=a.output_dir/(src.stem+'.png')
    subprocess.run([renderer,'--width','1500','--output',str(dest),str(src)],check=True)
    print(f'PASS rendered {src.name} -> {dest}')
print(f'PASS: {len(files)} SVGs rendered')
