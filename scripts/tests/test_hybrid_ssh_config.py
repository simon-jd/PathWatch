"""SSH argv regression only; no SSH connections, real inventory or private keys."""
from pathlib import Path
import shlex
import sys
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from hybrid_runtime import Runtime

class SshConfigTests(unittest.TestCase):
    def runtime(self):
        # Bypass private-input initialization: command construction needs only
        # synthetic endpoint values and path strings, never actual files.
        runtime=Runtime.__new__(Runtime)
        runtime.config={'nva_public_ip':'8.8.8.8','client_public_ip':'1.1.1.1'}
        runtime.key=ROOT/'.cache/test-only-identity'
        runtime.known=ROOT/'.cache/test-only-known-hosts'
        return runtime
    def test_both_azure_commands_ignore_global_config_and_use_only_project_identity(self):
        runtime=self.runtime()
        for node,host in [('azure','8.8.8.8'),('azure-client','1.1.1.1')]:
            command=runtime.command(node,['true'])
            self.assertEqual(command[:3],['ssh','-F','/dev/null'])
            options=[command[i+1] for i,arg in enumerate(command[:-1]) if arg=='-o']
            self.assertIn('IdentitiesOnly=yes',options)
            self.assertIn('StrictHostKeyChecking=yes',options)
            self.assertIn('GlobalKnownHostsFile=/dev/null',options)
            self.assertIn('UserKnownHostsFile='+str(runtime.known),options)
            self.assertEqual(command[command.index('-i')+1],str(runtime.key))
            self.assertEqual(command[-2:],[f'pathwatch@{host}','true'])
    def test_remote_shell_arguments_remain_quoted(self):
        args=['printf','%s','$(not-executed); private text']
        self.assertEqual(shlex.split(self.runtime().command('azure',args)[-1]),args)
    def test_inventory_example_declares_same_isolation_and_strict_project_trust(self):
        variables=yaml.safe_load((ROOT/'ansible/hybrid/inventory.yml.example').read_text())['all']['vars']
        options=shlex.split(variables['ansible_ssh_common_args'])
        self.assertEqual(options[:2],['-F','/dev/null'])
        for option in ('IdentitiesOnly=yes','BatchMode=yes','StrictHostKeyChecking=yes','GlobalKnownHostsFile=/dev/null','UserKnownHostsFile=/absolute/project/.cache/hybrid/known_hosts'):
            self.assertIn(option,options)
        self.assertEqual(variables['ansible_ssh_private_key_file'],'/absolute/project/.cache/hybrid/id_ed25519')
if __name__=='__main__':unittest.main()
