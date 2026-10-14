"""Exercise two real packaged chat requests and verify owned-worker cleanup."""
import hashlib
import json
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import urllib.request
import uuid

import psutil

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'.runtime'/('qwen-quality-package-'+uuid.uuid4().hex)
DEST.mkdir()

def save(name,value):
    with (DEST/name).open('x',encoding='utf-8') as f:json.dump(value,f,indent=2)

with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
engine=ROOT/'apps/desktop/src-tauri/binaries/ai-powerbi-studio-engine-x86_64-pc-windows-msvc.exe'
with (DEST/'stderr.log').open('x',encoding='utf-8') as log:
    process=subprocess.Popen([str(engine),'--port',str(port),'--emit-token','--state-root',str(DEST/'state')],
                             stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    lines=queue.Queue()
    def read():
        for line in process.stdout:lines.put(line)
    threading.Thread(target=read,daemon=True).start()
    results=[];owned=[]
    try:
        ready=json.loads(lines.get(timeout=90))
        def request(path,data=None,timeout=120):
            return urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{port}'+path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={'Authorization':'Bearer '+ready['token'],'Content-Type':'application/json'}),timeout=timeout)
        deadline=time.monotonic()+60
        while True:
            try:
                with request('/v1/health') as r:health=json.load(r)
                break
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.25)
        save('health-before.json',health)
        with request('/v1/workspaces',dict(name='Quality smoke',selected_folder=str(DEST),idempotency_key=uuid.uuid4().hex,expected_base_hash=hashlib.sha256(b'').hexdigest())) as r:
            workspace=json.load(r)
        save('workspace.json',workspace)
        for index,expected in enumerate(('Quality check ready.','Validation check ready.')):
            started=time.monotonic();events=[];first=None
            with request('/v1/chat/stream',{'workspace_id':workspace['id'],'message':'Reply with exactly this sentence: '+expected}) as r:
                for raw in r:
                    line=raw.decode('utf-8').strip()
                    if line.startswith('data: '):
                        item=json.loads(line[6:]);events.append(item)
                        if first is None and item.get('text'):first=time.monotonic()-started
            text=''.join(e.get('text','') for e in events)
            row=dict(index=index,expected=expected,text=text,exact=text.strip()==expected,
                     seconds=time.monotonic()-started,first_token_seconds=first,events=events)
            save(f'chat-{index}.json',row);results.append(row)
        with request('/v1/health') as r:save('health-after.json',json.load(r))
    except Exception as exc:
        save('failure.json',{'error':str(exc)})
        raise
    finally:
        if process.poll() is None:
            owned=psutil.Process(process.pid).children(recursive=True)
            process.terminate();process.wait(timeout=15)
        _,alive=psutil.wait_procs(owned,timeout=15)
        save('completion.json',{'chats':results,'remaining_owned_processes':[p.pid for p in alive]})
        if alive:
            for p in alive:p.kill()
            raise RuntimeError('Packaged engine left owned child processes alive')
print(DEST)
