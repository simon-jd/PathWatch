"""No live cloud: preparation must not copy state, metadata or credentials."""
import datetime as dt
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('prepare',ROOT/'scripts/prepare_azure_network_test.py')
prepare=importlib.util.module_from_spec(spec);spec.loader.exec_module(prepare)
SUB='11111111-1111-1111-1111-111111111111'

class FixturePreparationTests(unittest.TestCase):
    def setUp(self):
        self.end=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    def test_invalid_scope_ttl_or_peer_never_creates_fixture(self):
        for sub,region,cidr,ttl,peers in [
            ('00000000-0000-0000-0000-000000000000','westus','10.10.0.0/16',self.end,['8.8.8.8/32']),
            (SUB,'westus2','10.10.0.0/16',self.end,['8.8.8.8/32']),
            (SUB,'westus','10.30.0.0/16',self.end,['8.8.8.8/32']),
            (SUB,'westus','10.10.0.0/16','2020-01-01T00:00:00Z',['8.8.8.8/32']),
            (SUB,'westus','10.10.0.0/16',self.end,[]),
            (SUB,'westus','10.10.0.0/16',self.end,['0.0.0.0/0']),
            (SUB,'westus','10.10.0.0/16',self.end,['10.0.0.1/32']),
        ]:
            with self.subTest(region=region,cidr=cidr,peers=peers),self.assertRaises(ValueError):prepare.prepare(sub,region,cidr,ttl,peers)
    def test_shared_make_workflow_keeps_reviewed_budget_and_delete_guards(self):
        for relative in ['infra/envs/dev/azure-a','infra/envs/dev/azure-b','infra/test/fixtures/azure']:
            directory=ROOT/relative
            text=(directory/'Makefile').read_text()
            self.assertIn('include $(ROOT)/infra/mk/network.mk',text)
            self.assertNotIn('terraform -chdir',text)
            for target in ['plan','reserve','up','down-plan','down']:
                result=subprocess.run(['make','-n','-C',str(directory),target],text=True,capture_output=True,check=True)
                if target=='up':
                    self.assertLess(result.stdout.index('azure_budget.py check'),result.stdout.index('apply -input=false'))
                    self.assertIn('reviewed.tfplan',result.stdout)
                elif target=='down':
                    self.assertLess(result.stdout.index('azure_teardown.py'),result.stdout.index('apply -input=false'))
                    self.assertIn('--reviewed-sha256',result.stdout)
                elif target=='plan':
                    self.assertIn('-lockfile=readonly',result.stdout)
                    self.assertIn('-out="reviewed.tfplan"',result.stdout)
        run,fixture=prepare.prepare(SUB,'westus','10.10.0.0/16',self.end,['8.8.8.8/32'])
        self.addCleanup(shutil.rmtree,run)
        for target in ['plan','reserve','up','down']:
            result=subprocess.run(['make','-n','-C',str(fixture),'ROOT='+str(ROOT),'VARS=terraform.tfvars.json',target],text=True,capture_output=True,check=True)
            self.assertIn(str(ROOT),result.stdout)
            if target=='plan':self.assertIn('-var-file="terraform.tfvars.json"',result.stdout)
            if target=='up':self.assertIn('azure_budget.py check',result.stdout)
            if target=='down':self.assertIn('azure_teardown.py',result.stdout)
    def test_copies_only_source_and_generates_isolated_local_state_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as td:
            directory=Path(td)
            for relative in ['infra/modules/network-azure','infra/test/fixtures/azure']:
                target=directory/relative;target.mkdir(parents=True)
                (target/'main.tf').write_text('# synthetic source\n')
                (target/'.terraform.lock.hcl').write_text('# pinned fixture\n')
                for forbidden in ['terraform.tfstate','reviewed.tfplan','backend.tfbackend','credentials.json']:(target/forbidden).write_text('must never copy')
                (target/'.terraform').mkdir();(target/'.terraform'/'metadata').write_text('no')
            with patch.object(prepare,'ROOT',directory):
                run,fixture=prepare.prepare(SUB,'westus','10.10.0.0/16',self.end,['8.8.8.8/32'])
            self.assertTrue(str(run).startswith(str(directory/'.cache'/'azure-network-tests')))
            manifest=json.loads((run/'manifest.json').read_text())
            self.assertEqual(len(manifest['files']),5)
            self.assertEqual(json.loads((fixture/'terraform.tfvars.json').read_text())['name'],manifest['name'])
            copied={path.name for path in run.rglob('*') if path.is_file()}
            self.assertEqual(copied,{'main.tf','.terraform.lock.hcl','terraform.tfvars.json','manifest.json'})

if __name__=='__main__':unittest.main()
