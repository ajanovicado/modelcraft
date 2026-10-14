"""Fresh SQL confirmation after fixing function-call/schema confusion."""
import json
from pathlib import Path
import threading
import time
import uuid

import duckdb
from pydantic import BaseModel, ConfigDict
from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.settings import load_settings

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'.runtime'/('qualified-sql-eval-'+uuid.uuid4().hex);DEST.mkdir()
class Reply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    artifact:str
def save(name,value):
    with (DEST/name).open('x') as f:json.dump(value,f,indent=2)

cases=[
 dict(id='S1',prompt='Use DuckDB SQL. The table is named RouteLogs, with columns Area VARCHAR and Distance INTEGER. Rows are (North,8),(North,-3),(South,11). Return Area and SUM(Distance) AS TotalDistance grouped by Area, ordered Area.',
      ddl='CREATE TABLE RouteLogs(Area VARCHAR,Distance INTEGER)',table='RouteLogs',rows=[['North',8],['North',-3],['South',11]],columns=['Area','TotalDistance'],expected=[['North',5],['South',11]]),
 dict(id='S2',prompt='DuckDB CrewTime(Team VARCHAR, Minutes INTEGER) contains (Rust,4),(Rust,-1),(Teal,6). Return Team and SUM(Minutes) AS TotalMinutes grouped by Team, ordered Team.',
      ddl='CREATE TABLE CrewTime(Team VARCHAR,Minutes INTEGER)',table='CrewTime',rows=[['Rust',4],['Rust',-1],['Teal',6]],columns=['Team','TotalMinutes'],expected=[['Rust',3],['Teal',6]])]
save('tasks.json',dict(frozen_at=time.time(),cases=cases))
model=create_model_runtime(load_settings(ROOT/'config/defaults.yaml').model,DEST/'evidence')
out=[]
for case in cases:
    result=dict(id=case['id']);conn=None;timer=None
    try:
        answer=model.generate('Return the complete SQL artifact in the artifact string, with no Markdown fences. Use only supplied data and schema.',case['prompt'],Reply)
        result['artifact']=answer.artifact
        conn=duckdb.connect(config={'enable_external_access':'false','threads':1,'memory_limit':'256MB'})
        conn.execute(case['ddl']);conn.executemany('INSERT INTO '+case['table']+' VALUES (?,?)',case['rows'])
        stmts=conn.extract_statements(answer.artifact)
        if len(stmts)!=1 or stmts[0].type!=duckdb.StatementType.SELECT:raise ValueError('One SELECT required')
        timer=threading.Timer(3,conn.interrupt);timer.start()
        cur=conn.execute(answer.artifact)
        result['columns']=[x[0] for x in cur.description];result['rows']=[list(x) for x in cur.fetchmany(101)]
        result['correct']=result['rows']==case['expected'] and [x.casefold() for x in result['columns']]==[x.casefold() for x in case['columns']]
    except Exception as exc:
        result.update(error=str(exc),correct=False)
    finally:
        if timer:timer.cancel()
        if conn:conn.close()
    save(case['id']+'.json',result);out.append(result);print(json.dumps(result),flush=True)
model.write_attestation(DEST/'attestation.json');save('results.json',out)
print(DEST,flush=True)
