import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Database, FileCode2, Loader2, RefreshCw, Settings2 } from "lucide-react";
import {
  AUTO_DATE_TIME_STEPS, CULTURE_STEPS, modelOptionsOp, mQueryProblems, queryCheckView, queryParameterOp, REFRESH_DATA_WARNING, REFRESH_WARNING, refreshDataOp, tableQuery,
  tableQueryOp, visibleTables, type FactExpression, type QueryCheck, type RefreshFailure,
} from "./modelEditing";
import { QueueButton, TRUNCATED_NOTE, type ModelContext } from "./ModelEditorShared";

export type FullFactsState = "loading" | "ready" | "failed";

/** Power Query: each table's M text (edited only when Studio has it whole), its reload, the shared queries and parameters. */
export function ModelEditorQuery({ ctx, full, fullError, onRetry }: { ctx: ModelContext; full: FullFactsState; fullError?: string; onRetry: () => void }) {
  const tables = visibleTables(ctx.facts).filter(t => ctx.facts.partitions.some(p => p.table === t.name && p.source === "m"));
  const [selected, setSelected] = useState<string>();
  const table = tables.find(t => t.name === selected) ?? tables[0];
  const [refreshing, setRefreshing] = useState(false);
  return <div className="me-split">
    <nav className="me-list" aria-label="Queries">
      {tables.map(t => <button type="button" key={t.name} aria-current={table?.name === t.name ? "true" : undefined} onClick={() => setSelected(t.name)}>
        <Database size={14} aria-hidden="true" /><span>{t.name}<small>Power Query</small></span></button>)}
      {!tables.length && <p className="ie-note">No table of this model is loaded with Power Query.</p>}
      {ctx.facts.expressions.map(e => <button type="button" key={`expr:${e.name}`} aria-current={selected === `expr:${e.name}` ? "true" : undefined} onClick={() => setSelected(`expr:${e.name}`)}>
        <FileCode2 size={14} aria-hidden="true" /><span>{e.name}<small>{e.parameter ? "parameter" : "shared query"}</small></span></button>)}
    </nav>
    <section className="me-detail">
      {full === "loading" && <p className="ie-note" role="status"><Loader2 size={13} className="spin" aria-hidden="true" />Reading the queries in full…</p>}
      {full === "failed" && <p className="ie-note ie-error" role="alert">{fullError ?? "Studio could not read the queries in full."} <button type="button" className="secondary" onClick={onRetry}>Try again</button></p>}
      {full === "ready" && (selected?.startsWith("expr:")
        ? (() => { const expression = ctx.facts.expressions.find(e => `expr:${e.name}` === selected); return expression ? <ExpressionEditor key={selected} ctx={ctx} expression={expression} /> : null; })()
        : table ? <TableQueryEditor key={table.name} ctx={ctx} table={table.name} /> : null)}
      <details className="me-section" open={refreshing} onToggle={e => setRefreshing(e.currentTarget.open)}>
        <summary><RefreshCw size={13} aria-hidden="true" />Refresh the data</summary>
        <p className="ie-note">{REFRESH_DATA_WARNING}</p>
        <div className="me-actions">
          {table && <QueueButton ctx={ctx} op={refreshDataOp(table.name)} label={`Queue: refresh ${table.name}`} />}
          <QueueButton ctx={ctx} op={refreshDataOp()} label="Queue: refresh all tables" />
        </div>
      </details>
    </section>
  </div>;
}

/** How long the editor waits after the last keystroke before it asks the server's Power Query parser. */
export const QUERY_CHECK_DELAY_MS = 800;

type QueryCheckState = { text?: string; running: boolean; result?: QueryCheck; error?: string };

/** The server's Power Query check of `text` (`POST .../model/validate-query`), run on demand and after typing settles. */
function useQueryCheck(validate: ModelContext["validateQuery"], text: string, table: string, enabled: boolean) {
  const [state, setState] = useState<QueryCheckState>({ running: false });
  const request = useRef(0);
  const run = useCallback(async (value: string) => {
    if (!validate || !value.trim() || value.length > 50000) return;
    const mine = ++request.current;
    setState({ text: value, running: true });
    try {
      const result = await validate(value, table);
      if (mine === request.current) setState({ text: value, running: false, result });
    } catch (reason) {
      if (mine === request.current) setState({ text: value, running: false, error: reason instanceof Error ? reason.message : "Studio could not check this query." });
    }
  }, [validate, table]);
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setTimeout(() => void run(text), QUERY_CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [enabled, run, text]);
  // An answer about older text says nothing about what is in the editor now.
  return { run, current: state.text === text ? state : { running: false } as QueryCheckState };
}

function TableQueryEditor({ ctx, table }: { ctx: ModelContext; table: string }) {
  const query = tableQuery(ctx.facts, table);
  const [text, setText] = useState(query?.text ?? "");
  const [ifFails, setIfFails] = useState<RefreshFailure>("ask");
  const changed = Boolean(query) && text.replace(/\r\n/g, "\n").trim() !== (query?.text ?? "").replace(/\r\n/g, "\n").trim();
  const check = useQueryCheck(ctx.validateQuery, text, table, Boolean(query?.editable) && changed && Boolean(text.trim()));
  if (!query) return <p className="ie-note">Studio could not find this table's query.</p>;
  const server = check.current.result;
  const view = server ? queryCheckView(server) : undefined;
  // Microsoft's parser (when it ran) has the last word; Studio's own quick look only stands in until it answers.
  const problems = changed && !(server && server.checked) ? mQueryProblems(text) : [];
  const ready = changed && (server ? server.valid : true) && !problems.length;
  return <section className="me-editor" aria-label={`Power Query of ${table}`}>
    <h3><Database size={14} aria-hidden="true" />{table}</h3>
    {!query.editable ? <><p className="ie-note">{query.reason}</p>{query.text && <pre className="me-code" aria-label="Power Query (M), shortened">{query.text}</pre>}</>
      : <>
        <label className="ie-field-block">Power Query (M)
          <textarea className="me-code-input" value={text} rows={14} spellCheck={false} disabled={ctx.locked} onChange={e => setText(e.target.value)} /></label>
        {problems.map(p => <p key={p} className="ie-note ie-error" role="alert">{p}</p>)}
        <div className="me-query-check" aria-live="polite">
          {check.current.running && <p className="ie-note" role="status"><Loader2 size={13} className="spin" aria-hidden="true" />Checking the query…</p>}
          {view && <ul className={`me-query-result ${view.tone}`} aria-label="Query check">{view.lines.map(line => <li key={line}>
            {view.tone === "ok" ? <CheckCircle2 size={12} aria-hidden="true" /> : <AlertTriangle size={12} aria-hidden="true" />}{line}</li>)}</ul>}
          {check.current.error && <p className="ie-note ie-error" role="alert">{check.current.error}</p>}
        </div>
        <p className="ie-note">{REFRESH_WARNING}</p>
        <label className="ie-field-block">If the reload fails<select value={ifFails} disabled={ctx.locked} onChange={e => setIfFails(e.target.value as RefreshFailure)}>
          <option value="ask">Ask me</option><option value="keep">Keep the new query without its data</option><option value="undo">Undo the change</option></select></label>
        <div className="me-actions">
          {ctx.validateQuery && <button type="button" className="secondary" disabled={!text.trim() || check.current.running} onClick={() => void check.run(text)}>Check</button>}
          <QueueButton ctx={ctx} op={ready ? tableQueryOp(table, text, ifFails) : undefined} label="Queue this query" />
          {changed && <button type="button" className="secondary" onClick={() => setText(query.text)}>Reset</button>}
        </div>
      </>}
  </section>;
}

/** The value of a Power Query parameter as its M says it: `"East" meta [...]` gives East. */
export function parameterValue(expression: string | undefined): string {
  const head = (expression ?? "").split(/\bmeta\b/i)[0].trim();
  const quoted = /^"((?:[^"]|"")*)"$/.exec(head);
  return quoted ? quoted[1].replaceAll("\"\"", "\"") : head;
}

function ExpressionEditor({ ctx, expression }: { ctx: ModelContext; expression: FactExpression }) {
  const [value, setValue] = useState(parameterValue(expression.expression));
  if (expression.truncated) return <p className="ie-note">{TRUNCATED_NOTE}</p>;
  if (!expression.parameter) {
    return <section className="me-editor" aria-label={`Query ${expression.name}`}>
      <h3><FileCode2 size={14} aria-hidden="true" />{expression.name}</h3>
      {expression.description && <p className="ie-note">{expression.description}</p>}
      <pre className="me-code" aria-label="Power Query (M)">{expression.expression ?? ""}</pre>
      <p className="ie-note">A shared query that is not loaded as a table. Change it in Power BI Desktop (Home &gt; Transform data).</p>
    </section>;
  }
  const current = parameterValue(expression.expression);
  return <section className="me-editor" aria-label={`Parameter ${expression.name}`}>
    <h3><FileCode2 size={14} aria-hidden="true" />{expression.name}</h3>
    {expression.description && <p className="ie-note">{expression.description}</p>}
    <label className="ie-field-block">Value<input value={value} disabled={ctx.locked} onChange={e => setValue(e.target.value)} /></label>
    <p className="ie-note">The parameter keeps its type and suggested values. The new value is used at the next refresh.</p>
    <QueueButton ctx={ctx} op={value !== current ? queryParameterOp(expression.name, value) : undefined} label="Queue the new value" />
  </section>;
}

/** Model settings: discourage implicit measures (a change), culture and auto date/time (Desktop's own steps). */
export function ModelEditorSettings({ ctx }: { ctx: ModelContext }) {
  const settings = ctx.facts.settings;
  const [discourage, setDiscourage] = useState(Boolean(settings.discourageImplicitMeasures));
  return <section className="me-editor me-settings" aria-label="Model settings">
    <h3><Settings2 size={14} aria-hidden="true" />Model settings</h3>
    <label className="me-toggle"><input type="checkbox" checked={discourage} disabled={ctx.locked} onChange={e => setDiscourage(e.target.checked)} />Discourage implicit measures</label>
    <p className="ie-note">Report authors then use your measures instead of dragging number columns in to be summed.</p>
    <QueueButton ctx={ctx} op={modelOptionsOp(settings, { discourageImplicitMeasures: discourage })} />
    <dl className="me-facts">
      <dt>Auto date/time</dt><dd>{settings.autoDateTime === null || settings.autoDateTime === undefined ? "Not known" : settings.autoDateTime ? "On" : "Off"}<small>{AUTO_DATE_TIME_STEPS}</small></dd>
      <dt>Language (culture)</dt><dd>{settings.culture ?? "Not known"}<small>{CULTURE_STEPS}</small></dd>
      {settings.defaultMode && <><dt>Storage mode</dt><dd>{settings.defaultMode}</dd></>}
    </dl>
  </section>;
}
