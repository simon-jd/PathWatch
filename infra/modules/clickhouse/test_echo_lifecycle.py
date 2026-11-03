"""Optional built-artifact restart integration, explicitly selected by ECHO_BINARY."""
import hashlib,importlib.util,os,pathlib,socket,subprocess,tempfile,unittest,uuid
ROOT=pathlib.Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('home_lifecycle',pathlib.Path(__file__).with_name('home.py'));home=importlib.util.module_from_spec(spec);spec.loader.exec_module(home)
class RestartTests(unittest.TestCase):
 @unittest.skipUnless(os.environ.get('PATHWATCH_ECHO_BINARY'),'requires explicitly built echo artifact')
 def test_retained_fixture_pems_load_only_after_generation_tls_readiness(self):
  with tempfile.TemporaryDirectory(dir=ROOT/'.cache') as tmp:
   base=pathlib.Path(tmp);cert,key,ready=base/'cert.pem',base/'key.pem',base/'ready.json'
   with socket.socket() as sock:sock.bind(('127.0.0.1',0));address='127.0.0.1:'+str(sock.getsockname()[1])
   previous=None
   for _ in range(2):
    token=uuid.uuid4().hex
    with open(base/'echo.log','ab') as log:
     child=subprocess.Popen([os.environ['PATHWATCH_ECHO_BINARY'],'-listen',address,'-cert',str(cert),'-key',str(key),'-ready-file',str(ready),'-ready-token',token],stdout=log,stderr=log)
     try:
      home.wait_echo_ready(cert,ready,token,address,lambda:child.poll() is None)
      current=hashlib.sha256(cert.read_bytes()).hexdigest();self.assertNotEqual(current,previous);previous=current
     finally:
      child.terminate();self.assertEqual(child.wait(timeout=5),0)
if __name__=='__main__':unittest.main()
