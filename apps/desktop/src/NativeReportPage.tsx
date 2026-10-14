import { useCallback, useEffect, useState } from "react";
import { FileBarChart2, Loader2, RefreshCw } from "lucide-react";
import { api } from "./api";

type PagePreview = { image: string; captured_at: string; cached: boolean; page_id: string };
const pendingPreviews = new Map<string, Promise<PagePreview>>();
function requestPreview(path: string) {
  const pending = pendingPreviews.get(path);
  if (pending) return pending;
  const request = api<PagePreview>(path, { method: "POST" }).finally(() => pendingPreviews.delete(path));
  pendingPreviews.set(path, request);
  return request;
}

export function NativeReportPage({ workspaceId, pageId, active, onOpenDesktop }: {
  workspaceId: string; pageId: string; active: boolean; onOpenDesktop: () => Promise<void>;
}) {
  const [preview, setPreview] = useState<PagePreview>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const load = useCallback(async (refresh = false, signal?: AbortSignal) => {
    setLoading(true); setError("");
    try {
      const result = await requestPreview(`/v1/workspaces/${workspaceId}/imported-report/pages/${encodeURIComponent(pageId)}/preview?refresh=${refresh}`);
      if (!signal?.aborted) setPreview(result);
    } catch (reason) {
      if (!signal?.aborted) setError(reason instanceof Error ? reason.message : "The report preview could not load.");
    } finally { if (!signal?.aborted) setLoading(false); }
  }, [workspaceId, pageId]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(false, controller.signal);
    return () => controller.abort();
  }, [active, load]);
  return <section className="native-report-page" aria-label="Power BI report preview" aria-busy={loading}>
    <header><div><strong>Power BI preview</strong><small>{preview ? `Captured ${new Date(preview.captured_at).toLocaleString()}` : "Original charts, formatting and calculations"}</small></div>
      <button type="button" className="secondary" disabled={loading} onClick={() => void load(true)}>{loading ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}Refresh preview</button></header>
    {error && <div className="native-preview-error" role="alert"><p>{error}</p><button className="secondary" disabled={loading} onClick={async () => { await onOpenDesktop(); await load(true); }}><FileBarChart2 size={14} />Open Power BI &amp; retry</button></div>}
    <div className="native-preview-scroll">{preview ? <img src={preview.image} alt="Report page rendered by Power BI Desktop, including charts, cards and tables" /> : <div className="native-preview-empty" role="status">{loading ? <><Loader2 className="spin" size={24} /><p>Rendering the original report in Power BI…</p></> : <p>Open the working copy in Power BI to populate the preview.</p>}</div>}</div>
    <footer>Page snapshot from Power BI Desktop. Change slicers or edit in Desktop, then refresh here. Use Layout &amp; fields to inspect visual bindings.</footer>
  </section>;
}
