# Interview walkthrough

## Project in one paragraph

Modelcraft is a Windows workspace for local analysis and Power BI authoring.
It combines a Tauri/React client, authenticated Python sidecar, local model and
deterministic report engine. Its central design problem is turning a
conversational proposal into a bounded, reviewable, reversible action while
separately verifying correctness.

Use the [synthetic demo](demo.md) and identify its fixed replies as examples.
Distinguish code-level tests, live-model evidence, Desktop results and installed
application acceptance.

## Decisions to explain

| Decision | Implementation | Discussion prompt |
| --- | --- | --- |
| Native shell plus Python engine | [`lib.rs`](../apps/desktop/src-tauri/src/lib.rs), [`__main__.py`](../src/ai_powerbi_analyst/studio/__main__.py) | Why split the client and engine? How does the parent own the child lifetime? |
| Authenticated loopback | [`api.py`](../src/ai_powerbi_analyst/studio/api.py) | How are callers authenticated? Why are development origins explicit? |
| Typed plans | [`contracts.py`](../src/ai_powerbi_analyst/studio/contracts.py), [`planner.py`](../src/ai_powerbi_analyst/studio/planner.py) | Which model outputs can become executable behavior? |
| Bounded data work | [`aggregates.py`](../src/ai_powerbi_analyst/studio/aggregates.py), [`data_access.py`](../src/ai_powerbi_analyst/studio/data_access.py) | How are joins, memory, row limits and timeouts handled? |
| Durable state and retries | [`state.py`](../src/ai_powerbi_analyst/studio/state.py), [`state_owner.py`](../src/ai_powerbi_analyst/studio/state_owner.py) | How do retries and stale state avoid duplicate changes? |
| Reviewed, reversible edits | [`report_edits.py`](../src/ai_powerbi_analyst/studio/report_edits.py), [`report_revisions.py`](../src/ai_powerbi_analyst/studio/report_revisions.py) | How does failure preserve the original and support undo? |
| Reliable model setup | [`model_install.py`](../src/ai_powerbi_analyst/llm/model_install.py), [`tests`](../tests/unit/test_model_install.py) | How do repeated clicks, corruption and interruptions recover? |
| Separate preview/native verification | [`validation.py`](../src/ai_powerbi_analyst/powerbi/validation.py), [release gates](release-checklist.md) | What can be checked headlessly, and what requires Desktop? |

## Explain a failure path

Choose a regression test, read its setup/assertion and show the enforcing code.
Good examples are duplicate setup requests, a damaged model of the right size,
a report edit with a stale base hash, and recovery after an interrupted write.
Explain the invariant and why the test fails without it.

Development used AI assistance. Describe your own understanding, decisions and
validation work accurately; inspectable behavior is the useful evidence.

## Limits and next steps

The fixture uses fixed replies. Structural/parser checks do not prove numerical
DAX correctness or Desktop rendering. Some connectors/modes need separate native
qualification. Source publication does not imply installer qualification.
Discuss a concrete next improvement and how you would measure its success.
