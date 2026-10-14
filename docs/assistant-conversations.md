# Assistant conversations

Studio shows one Local AI assistant beside the workbench. Its header identifies the active context:

- **Workspace** on Report, Data and Model: ask about the workspace, discuss development questions, and prepare report changes.
- **DAX Lab**: discuss calculations, explain the current formula, and draft or test DAX using the imported schema.

Switching tabs preserves each chat, typed question and DAX scratchpad. Hiding the assistant keeps its state; Explain in the formula editor reveals the DAX assistant again. The contexts have separate request routes and histories, so a report conversation does not silently become formula context.

## History and New chat

Chat history lists current and saved chats from both contexts, with Workspace and DAX Lab labels and optional filters. Search includes the messages. Opening a history entry selects its context. Saved chats are read only; Return to current chat restores the active composer.

New chat affects the active context. It archives that conversation and excludes its messages from subsequent model requests. The DAX scratchpad and an unsent typed question remain available. New chat also detaches an explicit Workspace handoff, including when there are no DAX messages to archive. Starting an empty chat without a handoff does not create an empty archive.

Chats use the existing workspace event/message store. Previous-data conversations remain readable without becoming current model context. Deleting a workspace clears its device chat caches for every snapshot.

## Moving a formula to DAX Lab

Continue in DAX Lab carries the selected candidate formula and its originating question. The assistant shows **From Workspace chat**, the request, collapsed Source formula details, and Back to source chat. The imported schema is supplied for the same workspace and snapshot. Only this explicit handoff is included; unrelated Workspace messages and archives are excluded.

Use formula updates and previews the scratchpad. Saving or queueing the measure remains a separate action. Report changes still require the applicable review/build or imported-report delivery flow.

The device retains bounded handoff metadata across reloads. Back to source chat locates the originating response even if its Workspace chat has since been archived. Detach source request removes this context. A storage failure is visible and the active in-memory handoff remains usable.

## Calculation outcomes

Responses distinguish Formula ready to test, Needs clarification and incomplete/failed requests. Clarifications state that the current formula is retained. Evaluation errors stay with the editor; a rejected request does not make the adjacent old formula a newly generated result.

The fixed-grain recipe supports one grouping column from the metric's single source table, with an explicit decision to ignore other filters. It compiles checked ALLEXCEPT DAX, retaining that grouping and removing other source-table and expanded-table filters. Requests for multiple groups, related-table grouping, additional retained filters or mixed calculations require clarification. A percentage/share pattern cannot satisfy a fixed-total request. A clarification follow-up resolves only the immediately preceding matching request.

An open-ended request for the most complex DAX asks for a useful business purpose before generating a calculation.

## Request lifecycle and model memory

Each request captures its workspace, snapshot, active chat boundary and original input. Recovery retries keep that envelope and request ID; sending another question creates a new request. Try again after a completed Workspace answer starts a new request using that answer's own preceding question and the current attached context. The frontend admits one conversation operation at a time before rendering disabled controls, and only its owner can publish callbacks, reconcile saved messages or release the busy state. Workspace changes reset history navigation and invalidate old operations. Saving a DAX proposal also holds this guard until completion.

The engine checks the captured chat boundary when admitting a request and rejects an archived request ID. A New chat boundary is separate from the report/data hash, so unchanged report content does not make an old conversation current. Admission and boundary changes are atomic. Request receipts and the event/message store supply durable completion evidence; a completed response overrides an old browser recovery record.

Action receipts also record their original chat boundary; legacy receipts without that evidence cannot replay after an archive. A stale workspace save cannot restore an earlier boundary. Imported edit workers may record a receipt only for the reserved request ID and exact input. The engine entrypoint holds an OS lock on its state folder for its entire lifetime, preventing another engine process from bypassing the in-process conversation guards. Shutdown or process termination releases that ownership.

Stop cancels the transport without retrying it. Streaming ends at the terminal completion event, and buffered tokens or actions after Stop or completion are ignored. An incomplete response remains readable and copyable, is labeled as incomplete, and cannot be handed off as a completed DAX candidate. Failed or stopped exchanges do not become model memory for later questions.

Saved history and model input have separate purposes. Full stored chats remain available for review, while model history uses a bounded selection of recent user/assistant turns without starting on an orphan assistant reply. Workspace and snapshot boundaries are applied before that selection. Schema references, retrieved text, previous assistant output and source formulas are context; they do not grant permission to execute code or change a report.

Device caches use scoped, validated envelopes. DAX composer and request recovery are saved independently before the larger transcript cache, so a transcript quota failure does not suppress the smaller saves. Durable response fields outrank stale browser copies. A source-request change during inference prevents automatic formula replacement by the old response; its answer remains available for review.

These are implementation practices, not a certification. The design follows [Microsoft's chat-history and reduction guidance](https://learn.microsoft.com/en-us/semantic-kernel/concepts/ai-services/chat-completion/chat-history), [OpenAI's context-window guidance](https://developers.openai.com/api/docs/guides/conversation-state#managing-the-context-window), and [OWASP's guidance on separating instructions from context](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html#structured-prompts-with-clear-separation). The local engine retains its existing deterministic validation and explicit preview/save/review boundaries.

## Regression checks

- `npm test` in `apps/desktop`: conversation history, handoff association/storage, rendering, recovery and related frontend behavior.
- `npm run test:chat-browser`: isolated fixture APIs covering report chat, both histories, retry/reload isolation, one visible assistant, handoff/return, explicit preview, fresh-chat detachment, scroll preservation and responsive layouts. Lifecycle cases also cover rapid duplicate sends, transcript reconciliation before workspace switching, scoped archive navigation and incomplete-response actions. It performs no real workspace, model or Power BI writes.
- Focused Python compiler and DAX assistant regressions verify semantic intent, filter behavior, clarification follow-ups and unsupported combinations.
- Conversation/state and imported-edit regressions cover durable boundaries, completed-request replay, incomplete statuses, preparation failures, worker ownership and stale workspace saves. State-owner regressions cover concurrent startup, shutdown and forced termination.

These checks establish the interaction contract. Real-model qualification and installer/release gates remain tracked in [production readiness](production-readiness-20261004.md).
