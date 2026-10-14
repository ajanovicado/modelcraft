import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { BookOpen, Search } from "lucide-react";
import { api } from "./api";
import "./KnowledgePanel.css";

type Status = { documents: number; recipes: number; chunks: number; corpus_hash: string; trust_policy: string };
type Reference = { id: string; title: string; source_url: string; kind: string; text: string; sha256: string; verification: string };
type Receipt = { references: Reference[]; context_characters: number; corpus_hash: string };

export function SettingsWithKnowledge({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [result, setResult] = useState<Receipt>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const requestId = useRef(0);
  useEffect(() => {
    let active = true;
    api<Status>("/v1/knowledge").then(value => { if (active) setStatus(value); })
      .catch(() => { if (active) setError("Knowledge library unavailable. Restart the updated local engine, then reopen Settings."); });
    return () => { active = false; requestId.current += 1; };
  }, []);

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    const id = ++requestId.current;
    setLoading(true); setError(""); setResult(undefined);
    try {
      const receipt = await api<Receipt>(`/v1/knowledge/search?q=${encodeURIComponent(query.trim())}&mode=${mode}`);
      if (id === requestId.current) setResult(receipt);
    } catch (cause) {
      if (id === requestId.current) setError(cause instanceof Error ? cause.message : "Local search failed.");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  return <div className="settings-with-knowledge">{children}
    <section className="knowledge-panel" aria-labelledby="knowledge-title">
      <header><BookOpen size={22} /><div><h2 id="knowledge-title">Power BI knowledge</h2>
        <p>Local references for your AI. Search without asking the model or changing your report.</p></div></header>
      {status && <p className="knowledge-status">{status.documents} reference topics · {status.recipes} recipe guides · {status.chunks} searchable passages · Offline</p>}
      <form onSubmit={event => void search(event)} className="knowledge-search">
        <label>Search references<input value={query} onChange={event => setQuery(event.target.value)} maxLength={1000} placeholder="Try weighted average, LOD, or duplicate keys" /></label>
        <label>Reference type<select value={mode} onChange={event => setMode(event.target.value)}>
          <option value="hybrid">Docs and recipes</option><option value="docs">Documentation</option><option value="recipes">Recipe guides</option>
        </select></label>
        <button className="primary" disabled={loading || !query.trim()}><Search size={16} />{loading ? "Searching…" : "Search library"}</button>
      </form>
      <p className="knowledge-boundary">References guide the answer; they do not prove a formula works. AI responses are never automatically added as trusted knowledge. Formulas and relationships still require validation and approval.</p>
      {error && <p role="alert">{error}</p>}
      <div aria-live="polite">{result && <p>{result.references.length ? `${result.references.length} passages selected within the model's reference budget.` : "No matching references. Try a Power BI concept or function name."}</p>}</div>
      {result?.references.map(reference => <details className="knowledge-result" key={reference.id}>
        <summary>{reference.title}<span>{reference.kind === "recipe" ? "Recipe guide" : "Documentation"}</span></summary>
        <pre>{reference.text}</pre>
        <a href={reference.source_url} target="_blank" rel="noopener noreferrer">Open Microsoft reference (online)</a>
        <small>{reference.verification === "source_snapshot" ? "Local source snapshot" : "Reviewed reference guide"} · SHA-256 {reference.sha256.slice(0, 16)}… · Not engine validation</small>
      </details>)}
      {status && <small>Library fingerprint: {status.corpus_hash.slice(0, 24)}…</small>}
    </section>
  </div>;
}
