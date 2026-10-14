"""Restart-supervised local MVP API and independent worker."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mvp.config import settings,RUNTIME

parser=argparse.ArgumentParser()
parser.add_argument('--host',default='127.0.0.1')
parser.add_argument('--port',type=int,default=8767)
args=parser.parse_args();config=settings();RUNTIME.mkdir(exist_ok=True)
lock=(RUNTIME/'supervisor.lock').open('a+b')
if lock.tell()==0:lock.write(b'0');lock.flush()
lock.seek(0)
try:
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
except OSError:raise SystemExit('SalesCare MVP supervisor is already running.')
if config.get('postgres_home'):
    home=Path(config['postgres_home']);ctl=home/'pgsql/bin/pg_ctl.exe';data=home/'pgdata'
    if subprocess.run([str(ctl),'status','-D',str(data)],capture_output=True).returncode:
        with (home/'launch.log').open('a') as log:
            subprocess.run([str(ctl),'start','-D',str(data),'-l',str(home/'postgres.log'),'-w'],stdout=log,stderr=log,check=True,timeout=40)

stop=False
def halt(*_):
    global stop
    stop=True
signal.signal(signal.SIGINT,halt);signal.signal(signal.SIGTERM,halt)
processes={};logs={};last_start={};last_backup=0;backup_ok=True
marker=RUNTIME/'last-backup.json'
if marker.exists():
    try:
        previous=json.loads(marker.read_text('utf-8'))
        last_backup=previous.get('at',0);backup_ok=previous.get('ok',False)
    except (ValueError,OSError):pass
commands={'api':[sys.executable,'-m','uvicorn','mvp.app:app','--host',args.host,'--port',str(args.port),'--no-access-log','--no-proxy-headers'],
          'worker':[sys.executable,'-m','mvp.worker'],
          'channels':[sys.executable,'-m','mvp.channel_gateway'],
          'knowledge':[sys.executable,'-m','mvp.knowledge_worker']}
try:
    while not stop:
        for name,command in commands.items():
            old=processes.get(name)
            if old and old.poll() is None:continue
            if time.time()-last_start.get(name,0)<5:continue
            if name not in logs:logs[name]=(RUNTIME/(name+'.log')).open('a',encoding='utf-8')
            logs[name].write(time.strftime('\n%Y-%m-%d %H:%M:%S supervisor: starting '+name+'\n'));logs[name].flush()
            processes[name]=subprocess.Popen(command,cwd=ROOT,stdout=logs[name],stderr=logs[name],
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            last_start[name]=time.time()
        (RUNTIME/'pids.json').write_text(json.dumps({'supervisor':os.getpid(),**{n:p.pid for n,p in processes.items()}}),'utf-8')
        if config.get('postgres_home') and time.time()-last_backup>(86400 if backup_ok else 300):
            with (RUNTIME/'backup.log').open('a',encoding='utf-8') as log:
                backup=subprocess.run([sys.executable,'scripts/mvp_backup.py'],cwd=ROOT,stdout=log,stderr=log)
            backup_ok=backup.returncode==0
            last_backup=time.time()
            marker.write_text(json.dumps({'at':last_backup,'ok':backup.returncode==0}),'utf-8')
        time.sleep(2)
finally:
    for process in processes.values():
        if process.poll() is None:process.terminate()
    for process in processes.values():
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill()
    for file in logs.values():file.close()
