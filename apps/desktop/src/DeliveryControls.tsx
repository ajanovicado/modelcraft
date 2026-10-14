import { useState } from "react";
import { save } from "@tauri-apps/plugin-dialog";
import { CheckCircle2, Copy, FileCheck2, TriangleAlert } from "lucide-react";
import { EditorDialog } from "./EditorDialog";
import { defaultCopyName, deliveryStatus, type Delivery, type EditingClient } from "./importedEditing";

/**
 * "Edited in Studio · N changes not in your file · Save to file… / Save a copy…" (P10). Studio never
 * touches the original file silently: a copy is written next to the original by default and never
 * over an existing file, and replacing the original needs an explicit yes that names the file.
 */
export function DeliveryControls({ workspaceId, reportName, delivery, client, disabled, onChanged }: {
  workspaceId: string; reportName: string; delivery?: Delivery; client: EditingClient; disabled: boolean; onChanged: () => Promise<void> | void;
}) {
  const status = deliveryStatus(delivery);
  const [replacing, setReplacing] = useState(false);
  const [askingPath, setAskingPath] = useState<string>();
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string }>();

  const writeCopy = async (destination: string) => {
    setWorking(true); setMessage(undefined);
    const result = await client.saveCopy(workspaceId, destination);
    setWorking(false);
    if (result.ok) { setMessage({ tone: "ok", text: `Saved a copy: ${result.path}` }); await onChanged(); }
    else setMessage({ tone: "error", text: result.message });
  };
  const saveCopy = async () => {
    const suggested = defaultCopyName(delivery?.source_path, reportName);
    if ("__TAURI_INTERNALS__" in window) {
      try {
        const chosen = await save({ title: "Save a copy of the edited report", defaultPath: suggested, filters: [{ name: "Power BI report", extensions: ["pbix"] }] });
        if (typeof chosen === "string") await writeCopy(chosen);
      } catch (reason) { setMessage({ tone: "error", text: reason instanceof Error ? reason.message : "The save dialog could not open." }); }
    } else setAskingPath(suggested);
  };
  const replaceOriginal = async () => {
    setReplacing(false); setWorking(true); setMessage(undefined);
    const result = await client.replaceOriginal(workspaceId);
    setWorking(false);
    if (result.ok) { setMessage({ tone: "ok", text: `Saved to ${result.path}.${result.backup ? ` The previous file is kept as ${result.backup}.` : ""}` }); await onChanged(); }
    else setMessage({ tone: "error", text: `${result.message} Nothing was changed; Save a copy instead.` });
  };

  return <div className="ie-delivery">
    <span className={`ie-delivery-status ${status.tone}`} role="status">{status.tone === "ok" ? <CheckCircle2 size={14} aria-hidden="true" /> : <TriangleAlert size={14} aria-hidden="true" />}{status.text}</span>
    <button type="button" className="secondary" disabled={disabled || working || !status.canSaveToFile} title={status.canSaveToFile ? "Write these changes into your original file (a backup is kept)" : "Nothing to write, or the original changed: save a copy"} onClick={() => setReplacing(true)}><FileCheck2 size={14} />Save to file…</button>
    <button type="button" className="secondary" disabled={disabled || working || !status.canSaveCopy} title="Save the edited report as a new .pbix file" onClick={() => void saveCopy()}><Copy size={14} />Save a copy…</button>
    {message && <span className={`ie-delivery-message ${message.tone}`} role={message.tone === "error" ? "alert" : "status"}>{message.text}</span>}
    {replacing && <EditorDialog title="Replace your original file?" tone="warn" onCancel={() => setReplacing(false)}
      footer={<><button type="button" className="secondary" data-autofocus onClick={() => setReplacing(false)}>Cancel</button><button type="button" className="primary yellow" onClick={() => void replaceOriginal()}>Replace the file</button></>}>
      <p className="ie-dialog-text">This writes the edited report over <strong>{delivery?.source_path ?? "your original file"}</strong>.</p>
      <ul className="ie-dialog-list"><li>Studio keeps a backup of the current file next to it first.</li><li>It is refused if the file changed since you opened it here, or if it is open in Power BI.</li><li>The file opens normally in Power BI, which finishes its own housekeeping the next time you save it there.</li></ul>
    </EditorDialog>}
    {askingPath !== undefined && <EditorDialog title="Save a copy" onCancel={() => setAskingPath(undefined)}
      footer={<><button type="button" className="secondary" onClick={() => setAskingPath(undefined)}>Cancel</button><button type="button" className="primary yellow" disabled={!/\.pbix$/i.test(askingPath.trim())} onClick={() => { const path = askingPath.trim(); setAskingPath(undefined); void writeCopy(path); }}>Save copy</button></>}>
      <label className="ie-field-block">Where to save the copy
        <input data-autofocus value={askingPath} spellCheck={false} onChange={event => setAskingPath(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && /\.pbix$/i.test(askingPath.trim())) { event.preventDefault(); const path = askingPath.trim(); setAskingPath(undefined); void writeCopy(path); } }} /></label>
      <p className="ie-note">An existing file is never overwritten; a number is added to the name instead.</p>
    </EditorDialog>}
  </div>;
}
