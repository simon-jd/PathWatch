"""Task4 dedicated 0.25GB / 1M IO monitor, absolute counters including boot/apt."""
import datetime as dt
import json
import os
from pathlib import Path
import signal
import threading
import time
from hybrid_usage import enforce as usage_enforce

def enforce(sample):return usage_enforce(sample,egress_gb=.25,transactions=1000000)
class Monitor:
    def __init__(self,runtime,out):self.runtime=runtime;self.out=Path(out);self.stop=threading.Event();self.error=None
    def sample(self):
        if dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=90)>=self.runtime.expiry:raise ValueError('DNS cleanup margin reached; stop and delete')
        samples=self.runtime.usage()
        with (self.out/'usage.jsonl').open('a') as log:log.write(json.dumps({'monotonic_ns':time.monotonic_ns(),'realtime_ns':time.time_ns(),'guests':samples})+'\n')
        for sample in samples.values():enforce(sample)
    def __enter__(self):
        self.sample()  # Boot already over bound => stop before any setup/drill action.
        self.previous=signal.getsignal(signal.SIGUSR1)
        def failed(signum,frame):raise RuntimeError('DNS usage monitor: '+str(self.error))
        signal.signal(signal.SIGUSR1,failed)
        def watch():
            try:
                while not self.stop.wait(5):self.sample()
            except BaseException as error:
                self.error=error
                if not self.stop.is_set():os.kill(os.getpid(),signal.SIGUSR1)
        self.thread=threading.Thread(target=watch,daemon=True);self.thread.start();return self
    def __exit__(self,typ,value,tb):
        self.stop.set();self.thread.join(timeout=25);signal.signal(signal.SIGUSR1,self.previous)
        if self.thread.is_alive():raise RuntimeError('DNS monitor did not stop')
        if self.error is not None and typ is None:raise RuntimeError('DNS monitor failed: '+str(self.error))
        if typ is None:self.sample()  # Both SSH samples have bounded 10s deadlines; gate success after join.
