"""Fresh end-to-end app generation plus streaming smoke; immutable evidence."""
import json
from pathlib import Path
import time
import threading
import uuid

import duckdb
from pydantic import BaseModel, ConfigDict

from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.settings import load_settings

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'.runtime'/('qualified-app-eval-'+uuid.uuid4().hex)
DEST.mkdir()
def save(name,value):
    with (DEST/name).open('x',encoding='utf-8') as f:json.dump(value,f,indent=2)

class Reply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    artifact:str

TASKS=[
 dict(id='A1',domain='dax',prompt='Write a complete DAX measure named Net Distance for JourneyLog(Entry integer,Distance number). Sum visible Distance, including negative corrections, preserving current filter context.'),
 dict(id='A2',domain='sql',prompt='Use DuckDB SQL. The table is named WorkLog, with columns Team VARCHAR and Minutes INTEGER. Rows are (Gold,17),(Gold,-2),(Blue,9). Return Team and SUM(Minutes) AS TotalMinutes grouped by Team, ordered by Team.'),
 dict(id='A3',domain='m',prompt='Write complete Power Query M constructing typed table Samples(Key number,Reading number) with rows (9,6),(10,0),(11,-3). Keep only strictly positive Reading, preserve both columns, sorted by Key.'),
 dict(id='A4',domain='model',prompt='Depots(DepotKey,Name)=(10,South),(20,North). Visits(VisitKey,DepotKey)=(101,10),(102,10),(103,20). Identify the dimension primary key and fact foreign key, cardinality and standard safe filter direction.')]
save('tasks.json',dict(frozen_at=time.time(),tasks=TASKS))
model=create_model_runtime(load_settings(ROOT/'config/defaults.yaml').model,DEST/'evidence')
rows=[]
for task in TASKS:
    start=time.monotonic()
    row=dict(id=task['id'],domain=task['domain'])
    try:
        result=model.generate('Use only supplied schema and facts. Return the complete requested artifact in the artifact string, with no Markdown fences. Do not claim native execution.',task['prompt'],Reply)
        row['artifact']=result.artifact
        if task['domain']=='sql':
            conn=duckdb.connect(config={'enable_external_access':'false','threads':1,'memory_limit':'256MB'})
            timer=None
            try:
                conn.execute('CREATE TABLE WorkLog(Team VARCHAR,Minutes INTEGER)')
                conn.executemany('INSERT INTO WorkLog VALUES (?,?)',[('Gold',17),('Gold',-2),('Blue',9)])
                statements=conn.extract_statements(result.artifact)
                if len(statements)!=1 or statements[0].type!=duckdb.StatementType.SELECT:
                    raise ValueError('Smoke requires one read-only SELECT')
                timer=threading.Timer(3,conn.interrupt);timer.start()
                cur=conn.execute(result.artifact)
                row['columns']=[x[0] for x in cur.description];row['rows']=cur.fetchmany(101)
                row['execution_correct']=row['rows']==[('Blue',9),('Gold',15)] and [x.lower() for x in row['columns']]==['team','totalminutes']
            finally:
                if timer:timer.cancel()
                conn.close()
    except Exception as exc:
        row['error']=str(exc)
    row['seconds']=time.monotonic()-start
    rows.append(row);save(task['id']+'.json',row);print(json.dumps(row),flush=True)
try:
    chunks=list(model.stream_chat('Say only: Local model ready.',system_prompt='Follow the user instruction exactly.'))
    save('chat.json',dict(chunks=chunks,text=''.join(chunks),nonempty_chunks=len(chunks)))
    print(json.dumps({'chat':''.join(chunks),'chunks':len(chunks)}),flush=True)
except Exception as exc:
    save('chat-failure.json',dict(error=str(exc)))
model.write_attestation(DEST/'attestation.json')
save('results.json',rows)
print(DEST,flush=True)
