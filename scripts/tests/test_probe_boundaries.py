"""Offline external-boundary tests: no Azure, Docker, SSH or Terraform mutation."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import probe_preflight
import probe_transport_home as home
import probe_transport
import probe_usage_watch as watch
ROOT=Path(__file__).resolve().parents[2]
SUB='11111111-1111-1111-1111-111111111111'

class BoundaryTests(unittest.TestCase):
    def test_preflight_uses_only_exact_readonly_subscription_commands(self):
        responses=[{'id':SUB,'state':'Enabled'},[],[],{'name':'pinned'}]
        with patch.object(probe_preflight.subprocess,'check_output',side_effect=[json.dumps(v) for v in responses]) as run:
            result=probe_preflight.capture(SUB,'azure-b','Standard_B2ats_v2','24.04.202610210')
        self.assertEqual(result['location'],'swedencentral')
        self.assertEqual(result['image_urn'],'Canonical:ubuntu-24_04-lts:server:24.04.202610210')
        self.assertEqual([c[0][0][1:3] for c in run.call_args_list],[['account','show'],['vm','list-usage'],['vm','list-skus'],['vm','image']])
        for command in result['commands']:self.assertEqual(command[command.index('--subscription')+1],SUB)
        with patch.object(probe_preflight.subprocess,'check_output') as run:
            with self.assertRaises(ValueError):probe_preflight.capture(SUB,'azure-a','Standard_NC6','latest')
            run.assert_not_called()
    def test_foreign_existing_router_is_never_removed(self):
        with patch.object(sys,'argv',['home','down','--site','azure-a']),patch.object(home,'inspect',side_effect=[{'Config':{'Labels':{}}},None]),patch.object(home,'command') as run:
            with self.assertRaisesRegex(ValueError,'foreign'):home.main()
            run.assert_not_called()
    def test_second_owned_router_accepts_only_its_exact_bridge_routes(self):
        network={'Id':'a'*64,'Driver':'bridge','Scope':'local','Labels':home.LABELS,'IPAM':{'Config':[{'Subnet':'10.30.0.0/16','Gateway':'10.30.0.1'}]},'Containers':{'a':{}}}
        routes=[{'dst':'10.30.0.0/16','dev':'br-'+'a'*12},{'dst':'10.30.0.1','dev':'br-'+'a'*12,'type':'local'},{'dst':'10.30.255.255','dev':'br-'+'a'*12,'type':'broadcast'}]
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            directory=Path(tmp);files=probe_transport.render('azure-b','8.8.4.4',['A'*43+'=']*2,['B'*43+'=']*2,['C'*43+'=']*2,['D'*43+'=']*2)['home']
            for name,text in files.items():(directory/name).write_text(text)
            (directory/'manifest.json').write_text(json.dumps({'scope':'pathwatch-probe-transport','site':'azure-b','files':{n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()}}))
            calls=[]
            def command(args):
                calls.append(args)
                return json.dumps(routes) if args[0]=='ip' else 'owned-container'
            argv=['home','up','--site','azure-b','--config',str(directory),'--image-id','sha256:'+'b'*64]
            with patch.object(sys,'argv',argv),patch.object(home,'inspect',side_effect=[None,network,{'Config':{'Labels':{'project':'pathwatch'}}}]),patch.object(home,'command',side_effect=command):home.main()
            docker=[v for v in calls if v[0]=='docker'];self.assertEqual(len(docker),1)
            self.assertEqual(docker[0][docker[0].index('--network')+1],home.NETWORK)
            self.assertIn('10.30.0.3',docker[0]);self.assertNotIn('--privileged',docker[0]);self.assertNotIn('host',docker[0])
            routes[1]['dev']='foreign0'
            with patch.object(sys,'argv',argv),patch.object(home,'inspect',side_effect=[None,network,{'Config':{'Labels':{'project':'pathwatch'}}}]),patch.object(home,'command',side_effect=command):
                with self.assertRaises(ValueError):home.main()
    def test_watch_terminal_cannot_overwrite_private_key(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            key=Path(tmp)/'key';key.write_text('secret');key.chmod(0o600)
            known=Path(tmp)/'known';known.write_text('host')
            argv=['watch','--current-plan',tmp+'/current','--destroy-plan',tmp+'/destroy','--reviewed-destroy-sha256','a'*64,'--ssh-key',str(key),'--known-hosts',str(known),'--terminal-output',str(key)]
            with patch.object(sys,'argv',argv),patch.object(watch,'render') as render:
                with self.assertRaisesRegex(ValueError,'distinct'):watch.main()
                render.assert_not_called()
            self.assertEqual(key.read_text(),'secret')

if __name__=='__main__':unittest.main()
