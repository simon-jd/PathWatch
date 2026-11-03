#!/usr/bin/env python3
"""Build two explicit laboratory versions from one frozen SOURCE, without installs.
Run under the admitted Task14b build watchdog after race. No deployed release claim.
"""
import re,difflib,hashlib,json,os,pathlib,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parents[5]
source=sys.argv[1];suffix=('-'+sys.argv[2]) if len(sys.argv)>2 else ''
assert not suffix or re.fullmatch(r'-[a-z0-9-]+',suffix)
work=ROOT/('.cache/task14b/compat-source'+suffix);out=ROOT/('docs/evidence/raw/task14-core-rpc/compat-build-content'+suffix+'.json')
assert not work.exists() and not out.exists()
assert subprocess.check_output(['git','rev-parse',source],cwd=ROOT,text=True).strip()==source
files=subprocess.check_output(['git','ls-tree','-r','--name-only',source,'--','go.mod','go.sum','ingest','proto'],cwd=ROOT,text=True).splitlines()
files=[p for p in files if p in ['go.mod','go.sum'] or p.endswith(('.go','.proto','.sql')) and not p.endswith('_test.go')]
base={p:subprocess.check_output(['git','show',source+':'+p],cwd=ROOT) for p in files}
manifest={'source':source,'recipe':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),'base':{p:hashlib.sha256(b).hexdigest() for p,b in base.items()},'commands':[],'variants':{}}
def run(argv,cwd):
 p=subprocess.run([str(x) for x in argv],cwd=cwd,capture_output=True,text=True);manifest['commands'].append({'argv':[str(x) for x in argv],'cwd':str(cwd),'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr});out.write_text(json.dumps(manifest,indent=2)+'\n')
 if p.returncode:raise RuntimeError(p.stderr)
for version in ['v1.0','v1.1']:
 path=work/version
 for p,b in base.items():
  target=path/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b)
 if version=='v1.1':
  changes={
   'proto/pathwatch/core/v1/core.proto':('bool ready=7; }','bool ready=7; optional string build_note=8; }'),
   'ingest/internal/corerpc/service.go':('Minor: 0, Capabilities: Capabilities,','Minor: 1, BuildNote: proto.String("additive laboratory v1.1"), Capabilities: Capabilities | 16,'),
   'ingest/internal/corerpc/client.go':('"pathwatch-core-minor": "0"','"pathwatch-core-minor": "1"')}
  for p,(old,new) in changes.items():
   f=path/p;s=f.read_text();assert s.count(old)==1,(p,old);s=s.replace(old,new)
   if p.endswith('service.go'):s=s.replace('"context"','"context"\n "google.golang.org/protobuf/proto"')
   f.write_text(s)
  run([ROOT/'.cache/task13/go/bin/gofmt','-w',path/'ingest/internal/corerpc/service.go'],path)
  run(['/usr/bin/protoc','--proto_path=.','--proto_path=/usr/include','--plugin=protoc-gen-go='+str(ROOT/'.tools/bin/protoc-gen-go'),'--plugin=protoc-gen-go-grpc='+str(ROOT/'.tools/bin/protoc-gen-go-grpc'),'--go_out=.','--go_opt=paths=source_relative','--go-grpc_out=.','--go-grpc_opt=paths=source_relative','proto/pathwatch/core/v1/core.proto'],path)
  diff=''.join(''.join(difflib.unified_diff(base[p].decode().splitlines(True),(path/p).read_text().splitlines(True),fromfile='a/'+p,tofile='b/'+p)) for p in files if base[p]!=(path/p).read_bytes())
  (out.parent/('compat-additive'+suffix+'.patch')).write_text(diff)
 bindir=ROOT/'.cache/task14b'/(('bin' if version=='v1.0' else 'compat-bin')+suffix);bindir.mkdir()
 run(['go','build','-mod=readonly','-buildvcs=false','-p=2','-o',bindir/'ingest','./ingest/cmd/ingest'],path)
 if version=='v1.0':run(['go','build','-mod=readonly','-buildvcs=false','-p=2','-o',bindir/'fixture','./ingest/internal/corefixture/cmd'],path)
 manifest['variants'][version]={'inputs':{p:hashlib.sha256((path/p).read_bytes()).hexdigest() for p in files},'binaries':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in bindir.iterdir()}}
 for p in ['go.mod','go.sum','proto/pathwatch/v1/probe.proto','proto/pathwatch/v1/probe.pb.go','proto/pathwatch/v1/probe_grpc.pb.go']:assert (path/p).read_bytes()==base[p]
 out.write_text(json.dumps(manifest,indent=2)+'\n')
assert manifest['variants']['v1.0']['binaries']['.cache/task14b/bin'+suffix+'/ingest']!=manifest['variants']['v1.1']['binaries']['.cache/task14b/compat-bin'+suffix+'/ingest']
print(json.dumps({'source':source,'variants':{v:x['binaries'] for v,x in manifest['variants'].items()}}))
