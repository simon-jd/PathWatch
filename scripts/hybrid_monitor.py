"""Concurrent guest accounting during all potentially blocking measurement commands."""
import json
import os
from pathlib import Path
import signal
import threading
import time
from hybrid_usage import enforce
class Monitor:
    def __init__(self,runtime,out):self.runtime=runtime;self.out=Path(out);self.stop=threading.Event();self.thread=None;self.error=None
    def __enter__(self):
        # Main-thread SIGUSR1 interrupts a long command so its enclosing finally
        # restores impairment; remote measurement commands also carry own timeout.
        self.previous=signal.getsignal(signal.SIGUSR1)
        def failed(signum,frame):raise RuntimeError('Usage monitor stopped experiment: '+str(self.error))
        signal.signal(signal.SIGUSR1,failed)
        def sample():
            try:
                while not self.stop.is_set():
                    guests=self.runtime.usage()
                    with (self.out/'usage-watch.jsonl').open('a') as log:log.write(json.dumps({'monotonic_ns':time.monotonic_ns(),'realtime_ns':time.time_ns(),'guests':guests})+'\n')
                    for value in guests.values():enforce(value)
                    self.stop.wait(5)
            except BaseException as error:
                self.error=error
                if not self.stop.is_set():os.kill(os.getpid(),signal.SIGUSR1)
        self.thread=threading.Thread(target=sample,daemon=True);self.thread.start();return self
    def __exit__(self,typ,value,tb):
        self.stop.set();self.thread.join(timeout=45);signal.signal(signal.SIGUSR1,self.previous)
        if self.thread.is_alive():raise RuntimeError('Guest counter read did not stop within45s')
        if self.error is not None and typ is None:raise RuntimeError('Usage monitor failed: '+str(self.error))
