"""Each invocation owns a fresh run directory, including failed invocations."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile

@contextmanager
def evidence_run(output):
    root=Path(output);root.mkdir(parents=True,exist_ok=True)
    # --out is a collection, never a prior run to append or overwrite.
    run=Path(tempfile.mkdtemp(prefix='run-',dir=root))
    def status(value,error=None):
        data={'run_id':run.name,'status':value}
        if error is not None:data['error_type']=type(error).__name__
        temporary=run/'.run.json.tmp'
        temporary.write_text(json.dumps(data,indent=2)+'\n');temporary.replace(run/'run.json')
    status('running');print('Run evidence: '+str(run))
    try:
        yield run
    except BaseException as error:
        # A late monitor/cleanup failure can occur after a success file was
        # written. This newly owned run must never retain a PASS artifact.
        for name in ('acceptance.json','summary.json'):(run/name).unlink(missing_ok=True)
        status('failed',error);raise
    else:status('passed')
