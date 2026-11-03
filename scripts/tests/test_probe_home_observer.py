"""Owned namespace admission: complete Docker records, no daemon mutation."""
import copy
import importlib.util
import pathlib
import json
import hashlib
import tempfile
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[2]
PARENT='a'*64
NET='b'*64

def records():
    labels={'project':'pathwatch','owner':'codex-pathwatch','purpose':'probe-transport'}
    parent={'Id':PARENT,'Name':'/pathwatch-probe-home-azure-a','Config':{'Labels':labels},'State':{'Running':True},'HostConfig':{'NetworkMode':'pathwatch-probe-home','Privileged':False},'NetworkSettings':{'Networks':{'pathwatch-probe-home':{'NetworkID':NET,'IPAddress':'10.30.0.2','IPPrefixLen':16,'Gateway':'10.30.0.1'}}}}
    network={'Id':NET,'Name':'pathwatch-probe-home','Driver':'bridge','Scope':'local','Labels':labels,'Options':{},'IPAM':{'Config':[{'Subnet':'10.30.0.0/16','Gateway':'10.30.0.1'}]},'Containers':{PARENT:{'Name':'pathwatch-probe-home-azure-a','IPv4Address':'10.30.0.2/16'}}}
    return parent,network

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        path=ROOT/'scripts/probe_home_observer.py'
        self.assertTrue(path.exists(),'owned home namespace lifecycle missing')
        spec=importlib.util.spec_from_file_location('probe_home_observer',path)
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def test_exact_owned_parent_admitted_and_other_namespaces_refused(self):
        parent,network=records()
        self.m.validate_parent(parent,network,PARENT)
        mutations=[('Id','c'*64),('Name','/foreign'),('State',{'Running':False}),('Config',{'Labels':{'project':'pathwatch'}}),('HostConfig',{'NetworkMode':'host','Privileged':False}),('HostConfig',{'NetworkMode':'pathwatch-probe-home','Privileged':True}),('NetworkSettings',{'Networks':{'pathwatch-probe-home':{'NetworkID':'c'*64,'IPAddress':'10.30.0.2','IPPrefixLen':16,'Gateway':'10.30.0.1'}}}),('NetworkSettings',{'Networks':{'pathwatch-probe-home':{'NetworkID':NET,'IPAddress':'10.30.0.3','IPPrefixLen':16,'Gateway':'10.30.0.1'}}})]
        for key,value in mutations:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):self.m.validate_parent({**parent,key:value},network,PARENT)
        for expected in ['host','pathwatch-probe-home-azure-a','a'*12,'']:
            with self.subTest(expected=expected),self.assertRaises(ValueError):self.m.validate_parent(parent,network,expected)

    def test_bridge_identity_membership_and_ipam_fail_closed(self):
        parent,network=records()
        for key,value in [('Id','c'*64),('Labels',{}),('Driver','host'),('IPAM',{'Config':[{'Subnet':'10.30.0.0/16','Gateway':'10.30.0.9'}]}),('Containers',{}),('Options',{'com.docker.network.bridge.name':'foreign0'})]:
            with self.subTest(key=key),self.assertRaises(ValueError):self.m.validate_parent(parent,{**network,key:value},PARENT)

    def test_cleanup_rejects_foreign_or_reparented_children(self):
        labels={'project':'pathwatch','owner':'codex-pathwatch','purpose':'home-observer','pathwatch.parent':PARENT}
        child={'Id':'d'*64,'Config':{'Labels':labels},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        self.m.validate_child(child,PARENT)
        for change in [{'Id':'short'},{'Config':{'Labels':{**labels,'owner':'foreign'}}},{'HostConfig':{'NetworkMode':'container:'+'e'*64}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.m.validate_child({**child,**change},PARENT)

    def test_cleanup_attempts_all_exact_children_and_reports_failure(self):
        self.assertTrue(hasattr(self.m,'cleanup'),'bounded cleanup lifecycle missing')
        children=['d'*64,'e'*64]
        def inspect(kind,identity):return {'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT}},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        removed=[]
        def command(argv):
            removed.append(argv[-1])
            if argv[-1]==children[0]:raise RuntimeError('daemon failed')
            return ''
        with patch.object(self.m.transport,'inspect',side_effect=inspect),patch.object(self.m.transport,'command',side_effect=command):
            with self.assertRaisesRegex(RuntimeError,'daemon failed'):self.m.cleanup(PARENT,children)
        self.assertEqual(removed,children)

    def test_cleanup_stops_probe_before_echo_and_removes_only_stopped_children(self):
        echo,probe='d'*64,'e'*64;calls=[];running={echo:True,probe:True}
        def inspect(kind,identity):
            return {'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT,'component':'probe' if identity==probe else 'echo','pathwatch.stop-profile':'shutdown-seconds-max5-v1'}},'State':{'Running':running[identity],'ExitCode':0},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        def stop(argv,**kwargs):
            seconds=15 if argv[-1]==probe else 5
            self.assertEqual(argv,['docker','stop','--signal','SIGTERM','--timeout',str(seconds),argv[-1]])
            self.assertEqual(kwargs['timeout'],seconds+5)
            identity=argv[-1];calls.append(('stop',identity));running[identity]=False
            if identity==probe:self.assertTrue(running[echo],'echo must remain until probe final flush')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        def remove(argv):
            self.assertEqual(argv,['docker','rm',argv[-1]])
            self.assertFalse(running[argv[-1]])
            calls.append(('rm',argv[-1]));return ''
        with patch.object(self.m.transport,'inspect',side_effect=inspect),patch.object(self.m.transport,'command',side_effect=remove),patch('subprocess.run',side_effect=stop):self.m.cleanup(PARENT,[echo,probe])
        self.assertEqual(calls,[('stop',probe),('rm',probe),('stop',echo),('rm',echo)])

    def test_probe_stop_timeout_retains_live_probe_and_echo_for_recovery(self):
        probe,echo='e'*64,'d'*64
        def inspect(kind,identity):
            return {'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT,'component':'probe' if identity==probe else 'echo','pathwatch.stop-profile':'shutdown-seconds-max5-v1'}},'State':{'Running':True},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        with patch.object(self.m.transport,'inspect',side_effect=inspect),patch.object(self.m.transport,'command') as rm,patch('subprocess.run',side_effect=TimeoutError('client stop timeout')) as stop:
            with self.assertRaisesRegex(RuntimeError,'timeout'):self.m.cleanup(PARENT,[echo,probe])
        rm.assert_not_called();self.assertEqual(stop.call_count,1)

    def test_live_probe_without_stop_profile_is_retained(self):
        probe='e'*64
        child={'Id':probe,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT,'component':'probe'}},'State':{'Running':True},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        with patch.object(self.m.transport,'inspect',return_value=child),patch.object(self.m.transport,'command') as rm,patch('subprocess.run') as stop:
            with self.assertRaisesRegex(RuntimeError,'reviewed shutdown bound'):self.m.cleanup(PARENT,[probe])
        rm.assert_not_called();stop.assert_not_called()

    def test_probe_killed_by_stop_timeout_is_reported_not_success(self):
        probe='e'*64;running=True
        def inspect(kind,identity):
            return {'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT,'component':'probe','pathwatch.stop-profile':'shutdown-seconds-max5-v1'}},'State':{'Running':running,'ExitCode':137},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        def stop(*args,**kwargs):
            nonlocal running
            running=False;return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(self.m.transport,'inspect',side_effect=inspect),patch.object(self.m.transport,'command') as rm,patch('subprocess.run',side_effect=stop):
            with self.assertRaisesRegex(RuntimeError,'137'):self.m.cleanup(PARENT,[probe])
        rm.assert_called_once_with(['docker','rm',probe])

    def test_already_stopped_probe_failure_is_not_normal_cleanup_success(self):
        identity='e'*64
        child={'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT,'component':'probe'}},'State':{'Running':False,'ExitCode':137},'HostConfig':{'NetworkMode':'container:'+PARENT}}
        with patch.object(self.m.transport,'inspect',return_value=child),patch.object(self.m.transport,'command') as rm,patch('subprocess.run') as stop:
            with self.assertRaisesRegex(RuntimeError,'137'):self.m.cleanup(PARENT,[identity])
        stop.assert_not_called();rm.assert_called_once_with(['docker','rm',identity])

    def test_poll_detects_stopped_or_replaced_parent(self):
        self.assertTrue(hasattr(self.m,'check_parent'),'parent liveness monitor missing')
        parent,network=records()
        with patch.object(self.m.transport,'inspect',side_effect=[parent,network]):self.m.check_parent(PARENT)
        with patch.object(self.m.transport,'inspect',side_effect=[{**parent,'Id':'c'*64},network]):
            with self.assertRaises(ValueError):self.m.check_parent(PARENT)

    def test_daemon_error_is_not_missing_child(self):
        self.assertTrue(hasattr(self.m,'cleanup'),'bounded cleanup lifecycle missing')
        with patch.object(self.m.transport,'inspect',side_effect=ValueError('Cannot inspect owned Docker object: daemon unavailable')):
            with self.assertRaisesRegex(RuntimeError,'daemon unavailable'):self.m.cleanup(PARENT,['d'*64])

    def test_runtime_config_keeps_home_identity_paths_and_private_trust(self):
        self.assertTrue(hasattr(self.m,'prepare'),'private runtime configuration missing')
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            base=pathlib.Path(tmp)
            for name in ['probe','echo','ca.pem','cert.pem','key.pem','ingest.pem']:(base/name).write_text(name)
            settings={'parent_id':PARENT,'image_id':self.m.IMAGE,'probe_binary':str(base/'probe'),'echo_binary':str(base/'echo'),'probe_sha256':hashlib.sha256(b'probe').hexdigest(),'echo_sha256':hashlib.sha256(b'echo').hexdigest(),'ca':str(base/'ca.pem'),'cert':str(base/'cert.pem'),'key':str(base/'key.pem'),'ingest_ca':str(base/'ingest.pem')}
            probe={'probe_id':'home','spool_dir':str(base/'spool'),'metrics_addr':'10.30.0.2:9464','ingest':'10.30.0.1:19443','ingest_ca':str(base/'ingest.pem'),'targets':[]}
            (base/'probe.json').write_text(json.dumps(probe))
            cfg={'namespace_observer':settings,'probe_config':str(base/'probe.json'),'spool_dir':str(base/'spool')}
            result=self.m.prepare(cfg,base/'runtime')
            self.assertEqual(result['probe']['probe_id'],'home')
            self.assertEqual(result['probe']['spool_dir'],'/spool')
            self.assertEqual(result['probe']['ingest_ca'],'/trust/ingest.pem')
            for spelling in ['DNS','dns']:
                configured={**probe,'targets':[{spelling:{'transport':'doh','ca_file':settings['ca']}}]}
                (base/'probe.json').write_text(json.dumps(configured))
                mapped=self.m.prepare(cfg,base/'runtime')
                self.assertEqual(mapped['probe']['targets'][0][spelling]['ca_file'],'/trust/ca.pem')
                self.assertEqual(json.loads((base/'probe.json').read_text()),configured)
                configured['targets'][0][spelling]['ca_file']=settings['key']
                (base/'probe.json').write_text(json.dumps(configured))
                with self.assertRaisesRegex(ValueError,'CA'):self.m.prepare(cfg,base/'runtime')
            (base/'probe.json').write_text(json.dumps(probe))
            with self.assertRaisesRegex(ValueError,'separate'):self.m.prepare(cfg,base)
            for key,value in [('probe_id','fake-home'),('metrics_addr','0.0.0.0:9464'),('ingest','8.8.8.8:19443'),('development_cleartext',True),('shutdown_timeout','30s'),('shutdown_timeout','0s'),('shutdown_timeout',0),('shutdown_timeout','-1s'),('shutdown_timeout','NaNs'),('shutdown_timeout','5.1s'),('shutdown_timeout','500ms')]:
                (base/'probe.json').write_text(json.dumps({**probe,key:value}))
                with self.subTest(key=key),self.assertRaises(ValueError):self.m.prepare(cfg,base/'runtime')
            for value in ['', '0.5s', '5s']:
                (base/'probe.json').write_text(json.dumps({**probe,'shutdown_timeout':value}))
                with self.subTest(shutdown=value):self.m.prepare(cfg,base/'runtime')
            (base/'probe.json').write_text(json.dumps(probe))
            (base/'probe').write_text('changed artifact')
            with self.assertRaisesRegex(ValueError,'hash'):self.m.prepare(cfg,base/'runtime')

    def test_final_probe_identity_and_capabilities_fail_closed(self):
        self.assertTrue(hasattr(self.m,'validate_probe_process'),'final capability verifier missing')
        fields={'Name':'pathwatch-probe','Uid':' '.join([str(os.getuid())]*4),'Gid':' '.join([str(os.getgid())]*4),'NoNewPrivs':'1',**{k:'0000000000002000' for k in ['CapEff','CapPrm','CapInh','CapAmb','CapBnd']}}
        text=lambda values:'\n'.join(k+':\t'+v for k,v in values.items())
        self.m.validate_probe_process(text(fields))
        for key,value in [('Name','setpriv'),('Uid','0 0 0 0'),('CapBnd','00000000000020c0'),('CapEff','0'),('NoNewPrivs','0')]:
            with self.subTest(key=key),self.assertRaises(ValueError):self.m.validate_probe_process(text({**fields,key:value}))

    def test_evidence_write_failure_still_removes_captured_child(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
            base=pathlib.Path(tmp);parent,network=records();identity='d'*64
            prepared={'settings':{'parent_id':PARENT,'echo_binary':'/reviewed/echo','probe_binary':'/reviewed/probe','cert':'/private/cert','key':'/private/key','ca':'/private/ca','ingest_ca':'/private/ingest'},'directory':base/'runtime','spool':base/'spool','probe':{}}
            child={'Id':identity,'Config':{'Labels':{**self.m.LABELS,'pathwatch.parent':PARENT}},'HostConfig':{'NetworkMode':'container:'+PARENT}}
            def inspect(kind,name):
                if kind=='image':return {'Id':'sha256:456063a689194186633bb3db0862283068f4c2ee538852d3aaffa7eb66f1f841'}
                if kind=='network':return network
                return child if name==identity else parent
            removed=[]
            def command(argv):
                if argv[1]=='create':return identity
                if argv[1]=='rm':removed.append(argv[-1])
                return ''
            write=pathlib.Path.write_text
            def fail_state(path,*args,**kwargs):
                if path.name=='state.json':raise OSError('evidence disk failure')
                return write(path,*args,**kwargs)
            with patch.object(self.m,'prepare',return_value=prepared),patch.object(self.m.transport,'inspect',side_effect=inspect),patch.object(self.m.transport,'command',side_effect=command),patch.object(pathlib.Path,'write_text',fail_state):
                with self.assertRaisesRegex(OSError,'evidence disk failure'):self.m.run({},base/'runtime')
            self.assertEqual(removed,[identity])

    def test_lowercase_missing_object_is_distinct_from_daemon_failure(self):
        for error in ['Error: no such object: exact-id','Error response from daemon: No such container: exact-id']:
            with self.subTest(error=error),patch.object(self.m.transport.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='[]',stderr=error)):
                self.assertIsNone(self.m.transport.inspect('container','exact-id'))
        for error in ['permission denied','Cannot connect: no such file or directory','daemon unavailable']:
            with self.subTest(error=error),patch.object(self.m.transport.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr=error)),self.assertRaises(ValueError):self.m.transport.inspect('container','exact-id')

    def test_router_down_requires_observer_cleanup_before_parent_removal(self):
        parent,network=records();removed=[]
        with patch.object(sys,'argv',['router','down','--site','azure-a']),patch.object(self.m.transport,'inspect',side_effect=[parent,network]),patch('probe_home_observer.stop_for_parent',side_effect=RuntimeError('observer cleanup failed')),patch.object(self.m.transport,'command',side_effect=lambda argv:removed.append(argv)):
            with self.assertRaisesRegex(RuntimeError,'observer cleanup failed'):self.m.transport.main()
        self.assertEqual(removed,[])

if __name__=='__main__':unittest.main()
