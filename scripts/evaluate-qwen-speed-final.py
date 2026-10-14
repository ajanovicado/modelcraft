"""Fresh production-factory smoke; no benchmark-only runtime monkeypatches."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from pydantic import BaseModel, ConfigDict
from ai_powerbi_analyst.llm.factory import create_model_runtime
from ai_powerbi_analyst.settings import load_settings

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'.runtime'/('qwen-speed-final-'+uuid.uuid4().hex);DEST.mkdir()
class Reply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    artifact:str

tasks=[
    dict(id='V1',domain='sql',prompt='DuckDB CartCharges(Lane VARCHAR, Boxes INTEGER, Fee INTEGER) contains (R,5,6),(R,2,9),(S,3,7). Return Lane and SUM(Boxes * Fee) AS Cost grouped by Lane, ordered Lane.',tables={'CartCharges':{'columns':[['Lane','VARCHAR'],['Boxes','INTEGER'],['Fee','INTEGER']],'rows':[['R',5,6],['R',2,9],['S',3,7]]}},expected_columns=['Lane','Cost'],expected_rows=[['R',48],['S',21]]),
    dict(id='V2',domain='dax',prompt='Write a DAX measure named NetHeight summing JourneyHeights[Height]. JourneyHeights has JourneyKey and Height. Preserve current filter context. Return only the complete measure.',measure='NetHeight'),
    dict(id='V3',domain='m',prompt='Write a complete Power Query M expression starting with #table(type table [Key=Int64.Type, Units=Int64.Type, Rate=Int64.Type], {{6,5,7},{19,2,13}}). Add Amount = Units * Rate. Return the resulting table sorted by Key ascending.'),
    dict(id='V4',domain='model',prompt='Give the active Power BI relationship between AreasFinal(AreaKey,Name) and ConsignmentsFinal(ConsignmentKey,AreaKey,Boxes). AreasFinal[AreaKey] is unique and complete; ConsignmentsFinal[AreaKey] repeats and all values exist in AreasFinal. State both columns, cardinality, active status and single filter direction from dimension to fact.',schema={'AreasFinal':['AreaKey','Name'],'ConsignmentsFinal':['ConsignmentKey','AreaKey','Boxes']}),
]
tasks.append({'id': 'R1', 'domain': 'm', 'prompt': 'Write a complete Power Query M expression starting with #table(type table [Key=Int64.Type, Units=Int64.Type, Rate=Int64.Type], {{8,4,9},{16,3,11}}). Add Amount = Units * Rate. Return the resulting table sorted by Key ascending.'})

def save(name,obj):
    with (DEST/name).open('x',encoding='utf-8') as f:json.dump(obj,f,indent=2)
save('tasks.json',tasks)
save('script.json',{'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
model=create_model_runtime(load_settings(ROOT/'config/defaults.yaml').model,DEST/'evidence')
save('runtime.json',{'class':type(model).__name__})
rows=[]
try:
    for task in tasks:
        t=time.monotonic();row=dict(id=task['id'],domain=task['domain'])
        try:
            answer=model.generate('Return the complete requested artifact in the artifact string. No Markdown fences. Use only supplied schema and data.',task['prompt'],Reply)
            row['artifact']=answer.artifact
        except Exception as exc:row['error']=str(exc)
        row['end_to_end_seconds']=time.monotonic()-t
        rows.append(row);save(task['id']+'.json',row);print(json.dumps(row),flush=True)
    for i in range(2):
        t=time.monotonic();chunks=list(model.stream_chat('Reply with exactly: Ready.',system_prompt='Follow the current user instruction concisely.'))
        save('chat-'+str(i)+'.json',dict(chunks=chunks,seconds=time.monotonic()-t))
    model.write_attestation(DEST/'attestation.json')
finally:model.unload()
save('results.json',rows)
print(DEST,flush=True)
