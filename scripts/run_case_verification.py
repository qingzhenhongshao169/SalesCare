"""Run tests in a uniquely named PostgreSQL database and emit only redacted diagnostics."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.stdout.reconfigure(encoding='utf-8')

ROOT=Path(__file__).resolve().parents[1]
runtime=ROOT/'.mvp-runtime'
manifest=runtime/'case-verification-run.json'
name='salescare_case_test_'+uuid.uuid4().hex[:12]
run={'database':name,'config':str(runtime/(name+'-config.json'))}
manifest.write_text(json.dumps(run),encoding='utf-8')
assert re.fullmatch(r'salescare_case_test_[a-f0-9]{12}',run['database'])
env={**os.environ,'SALESCARE_TEST_DATABASE_NAME':run['database'],
     'SALESCARE_TEST_BASE_CONFIG':str(runtime/'case-test-postgres-20261019/base-config.json'),
     'PYTHONIOENCODING':'utf-8'}
args=sys.argv[1:] or ['mvp/tests/test_case_recommendations.py']
cmd=[sys.executable,'-m','pytest','-q','--tb=short',*args]
result=subprocess.run(cmd,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600)
log=result.stdout+'\n'+result.stderr
(runtime/'case-verification-tests.log').write_text(log,encoding='utf-8')
safe=re.sub(r'(postgresql(?:\+psycopg)?://)[^\s/@]+(?::[^\s/@]*)?@',r'\1[redacted]@',log)
safe=re.sub(r'(?i)(api_key|password|cipher|sealed)([\s\x27\x22:=]+)[^,\s}]+',r'\1\2[redacted]',safe)
print(safe[-18000:])
print(json.dumps({'exit_code':result.returncode,'isolated_database':run['database'],'raw_log':'protected runtime directory'}))
raise SystemExit(result.returncode)
