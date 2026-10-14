import { invoke } from "@tauri-apps/api/core";
import { apiErrorMessage } from "./apiErrors";
import type { ReportAction } from "./types";
import { parseChatEditEvent, type ChatEditEvent, type RawResponse } from "./importedEditing";
import type { AddDaxMeasureRequest, AdvancedDaxReviewRequest, BrowseResult, ClearChatRequest, DeleteWorkspaceRequest, Draft, DaxResult, DaxPreviewContext, DaxGroupValue, RelinkBuildRequest, RenameWorkspaceRequest, ReportThemePalette, SetWorkspaceFolderRequest, UpdateDashboardRequest, ValidationReceipt, Workspace, WorkspaceRemoval } from "./types";

type SidecarConnection = { endpoint: string; token: string };

let connection: Promise<SidecarConnection> | undefined;

async function resolveConnection(): Promise<SidecarConnection> {
  if (connection) return connection;
  connection = (async () => {
    try {
      return await invoke<SidecarConnection>("sidecar_connection");
    } catch {
      if (!import.meta.env.DEV) {
        throw new Error("The local engine could not be reached. Restart the desktop app. No development connection was attempted.");
      }
      const token = import.meta.env.VITE_STUDIO_TOKEN;
      if (typeof token !== "string" || token.length < 16) {
        throw new Error(
          "Set VITE_STUDIO_TOKEN (16+ characters) when running the browser development shell against a local engine.",
        );
      }
      return {
        endpoint: import.meta.env.VITE_STUDIO_API_URL || "http://127.0.0.1:8765",
        token,
      };
    }
  })().then((value) => {
    const url = new URL(value.endpoint);
    if (url.protocol !== "http:" || !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname !== "/"
      || typeof value.token !== "string" || value.token.length < 16) {
      throw new Error("The local engine returned an invalid connection. Only authenticated loopback connections are allowed.");
    }
    return value;
  }).catch((error: unknown) => {
    // A temporary startup failure must not poison every subsequent retry.
    connection = undefined;
    throw error;
  });
  return connection;
}

function assertStudioPath(path: string): void {
  if (!path.startsWith("/v1/")) {
    throw new Error("Local engine requests must target /v1/ routes.");
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  assertStudioPath(path);
  init?.signal?.throwIfAborted();
  const sidecar = await resolveConnection();
  init?.signal?.throwIfAborted();
  const options = {
    ...init,
    headers: {
      Authorization: `Bearer ${sidecar.token}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
  };
  let response: Response;
  try {
    response = await fetch(`${sidecar.endpoint}${path}`, options);
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    // Retry only read-only requests. Mutations use their explicit idempotency IDs.
    if (init?.method && init.method !== "GET") throw new Error(`Local engine request failed (${path}). Check the connection and retry.`);
    try { response = await fetch(`${sidecar.endpoint}${path}`, options); }
    catch (error) { if (init?.signal?.aborted) throw error; throw new Error(`Local engine disconnected while loading ${path}. Reload the workspace when it reconnects.`); }
  }
  init?.signal?.throwIfAborted();
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => undefined);
    init?.signal?.throwIfAborted();
    throw new Error(apiErrorMessage(payload, response.status));
  }
  const body = await response.json() as T;
  init?.signal?.throwIfAborted();
  return body;
}

/**
 * Like `api`, but returns the status and JSON body of any answer instead of throwing on an error
 * status: editing needs the `code` of a 409 or 422 (head_moved, desktop_unsaved, ...) to decide
 * what to ask the user. Network failures still throw, and a mutation is never retried here: the
 * edit client replays it with the same idempotency key when the user chooses to.
 */
export async function apiRaw(path: string, init?: RequestInit): Promise<RawResponse> {
  assertStudioPath(path);
  init?.signal?.throwIfAborted();
  const sidecar = await resolveConnection();
  init?.signal?.throwIfAborted();
  const options = {
    ...init,
    headers: { Authorization: `Bearer ${sidecar.token}`, "Content-Type": "application/json", ...init?.headers },
  };
  const mutation = Boolean(init?.method && init.method !== "GET");
  let response: Response;
  try {
    response = await fetch(`${sidecar.endpoint}${path}`, options);
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    if (mutation) throw new Error(`Local engine request failed (${path}). Check the connection and retry.`);
    try { response = await fetch(`${sidecar.endpoint}${path}`, options); }
    catch (error) { if (init?.signal?.aborted) throw error; throw new Error(`Local engine disconnected while loading ${path}. Reload the workspace when it reconnects.`); }
  }
  init?.signal?.throwIfAborted();
  const body: unknown = await response.json().catch(() => undefined);
  init?.signal?.throwIfAborted();
  return { status: response.status, body };
}

/** The transport the imported-report edit client runs on. */
export const editTransport = (method: "GET" | "POST", path: string, body?: unknown): Promise<RawResponse> =>
  apiRaw(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });

export async function streamChat(
  workspaceId: string,
  message: string,
  onToken: (token: string) => void,
  signal: AbortSignal,
  request?: { idempotency_key: string; expected_base_hash: string; chat_boundary?: number; onStatus?: (text: string) => void; onReportAction?: (action: ReportAction) => void; onEditEvent?: (event: ChatEditEvent) => void; context?: { page_id?: string; visual_id?: string; table_id?: string; measure_id?: string; relationship_id?: string } },
): Promise<string | undefined> {
  return streamAssistant("/v1/chat/stream", { workspace_id: workspaceId, message,
    idempotency_key: request?.idempotency_key, expected_base_hash: request?.expected_base_hash, chat_boundary: request?.chat_boundary,
    ...(request?.context ? { context: request.context } : {}) }, onToken, signal, request?.onStatus, undefined, request?.onReportAction, request?.onEditEvent);
}

export type DaxAssistInput = { goal: string; expression: string; error?: string; discussion?: string; mode: "auto" | "generate" | "explain" | "fix"; requestId?: string; chatBoundary?: number };
export type DaxAssistAnswer = { text: string; kind: "advice" | "explain" | "formula"; calculationRequest?: string; requestId?: string };

export async function streamDaxAssistant(workspaceId: string, snapshotId: string | undefined, input: DaxAssistInput,
  signal: AbortSignal, onStatus: (text: string) => void): Promise<DaxAssistAnswer> {
  const { requestId, chatBoundary, ...request } = input;
  const answer: DaxAssistAnswer = { text: "", kind: "advice", requestId: requestId || idempotencyKey() };
  let classified = false;
  await streamAssistant("/v1/dax/assist", { ...request, workspace_id: workspaceId, ...(snapshotId ? { snapshot_id: snapshotId } : {}),
    request_id: answer.requestId, ...(chatBoundary === undefined ? {} : { chat_boundary: chatBoundary }) }, token => { answer.text += token; }, signal, onStatus, payload => {
      if (payload.kind !== "advice" && payload.kind !== "explain" && payload.kind !== "formula") throw new Error("The assistant returned an unknown response type. Your formula is unchanged.");
      answer.kind = payload.kind;
      classified = true;
      if (typeof payload.calculation_request === "string" && payload.calculation_request.length <= 800) answer.calculationRequest = payload.calculation_request;
    });
  if (!classified) throw new Error("Restart the local engine to use the updated DAX assistant. Your formula is unchanged.");
  return answer;
}

async function streamAssistant(path: string, body: object, onToken: (token: string) => void,
  signal: AbortSignal, onStatus?: (text: string) => void,
  onAssistant?: (payload: { kind?: string; calculation_request?: string }) => void,
  onReportAction?: (action: ReportAction) => void,
  onEditEvent?: (event: ChatEditEvent) => void): Promise<string | undefined> {
  signal.throwIfAborted();
  const sidecar = await resolveConnection();
  signal.throwIfAborted();
  let response: Response;
  try { response = await fetch(`${sidecar.endpoint}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${sidecar.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  }); } catch (error) {
    if (signal.aborted) throw error;
    throw new Error("The local engine disconnected. Your question can be retried when it reconnects.");
  }
  if (!response.ok || !response.body) {
    signal.throwIfAborted();
    const failure = await response.json().catch(() => ({}));
    signal.throwIfAborted();
    throw new Error(apiErrorMessage(failure, response.status));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let draftId: string | undefined;
  try {
  while (true) {
    signal.throwIfAborted();
    const { value, done } = await reader.read();
    signal.throwIfAborted();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() || "";
    for (const event of events) {
      signal.throwIfAborted();
      const type = event.match(/^event: ([^\r\n]+)/m)?.[1];
      const data = event.match(/^data: ([^\r\n]+)/m)?.[1];
      if (!data) continue;
      const payload = JSON.parse(data) as { text?: string; message?: string; draft_id?: string; kind?: string; calculation_request?: string; page_id?: string; visual_id?: string };
      if (type === "token" && payload.text) onToken(payload.text);
      if (type === "status" && payload.text) onStatus?.(payload.text);
      if (type === "assistant") onAssistant?.(payload);
      if (type === "draft") draftId = payload.draft_id;
      if (type === "report_action" && payload.kind === "visual_date_filter") onReportAction?.({ kind: payload.kind,
        page_id: typeof payload.page_id === "string" ? payload.page_id : undefined,
        visual_id: typeof payload.visual_id === "string" ? payload.visual_id : undefined });
      if (type?.startsWith("edit_") || type === "explanation" || type === "test_suggestions") {
        // Progress, receipts, plans, questions and confirmations of an imported-report edit (docs/imported-report-editing-api.md),
        // and the judgement answers the chat draws as cards (an explained number, suggested report tests).
        const edit = parseChatEditEvent(type, payload);
        if (edit) onEditEvent?.(edit);
      }
      if (type === "done") return draftId;
      if (type === "error") throw new Error(payload.message || "Local model failed");
    }
  }
  throw new Error("The local connection ended before completion. Reload the draft before retrying a build request.");
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}

export function clearChat(workspaceId: string, request: ClearChatRequest): Promise<Workspace> {
  return api<Workspace>(`/v1/workspaces/${workspaceId}/chat/clear`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function setWorkspaceFolder(workspaceId: string, request: SetWorkspaceFolderRequest): Promise<Workspace> {
  return api<Workspace>(`/v1/workspaces/${workspaceId}/folder`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function renameWorkspace(workspaceId: string, request: RenameWorkspaceRequest): Promise<Workspace> {
  return api<Workspace>(`/v1/workspaces/${workspaceId}/name`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function deleteWorkspace(workspaceId: string, request: DeleteWorkspaceRequest): Promise<WorkspaceRemoval> {
  return api<WorkspaceRemoval>(`/v1/workspaces/${workspaceId}/delete`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function browseDirectory(path?: string): Promise<BrowseResult> {
  return api<BrowseResult>(`/v1/browse${path ? `?path=${encodeURIComponent(path)}` : ""}`);
}

export function evaluateDax(workspaceId: string, expression: string, topN = 50, preview?: DaxPreviewContext, detailPeriod?: string, signal?: AbortSignal, detailGroup?: { value: DaxGroupValue }, engine: "snapshot" | "powerbi" = "snapshot"): Promise<DaxResult> {
  return api<DaxResult>(`/v1/workspaces/${workspaceId}/dax/evaluate`, {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, expression, engine, top_n: topN, ...(preview ? { preview } : {}), ...(detailPeriod ? { detail_period: detailPeriod } : {}), ...(detailGroup ? { detail_group: detailGroup } : {}) }),
    signal,
  });
}

export function addDaxMeasure(workspaceId: string, request: AddDaxMeasureRequest): Promise<Draft> {
  return api<Draft>(`/v1/workspaces/${workspaceId}/dax/measures`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function updateDashboard(draftId: string, request: UpdateDashboardRequest): Promise<Draft> {
  return api<Draft>(`/v1/drafts/${draftId}/dashboard`, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export function reportThemes(): Promise<ReportThemePalette[]> {
  return api<ReportThemePalette[]>("/v1/report-themes");
}

export function reviewAdvancedDax(draftId: string, measureId: string, request: AdvancedDaxReviewRequest): Promise<Draft> {
  return api<Draft>(`/v1/drafts/${draftId}/measures/${measureId}/approval`, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export function relinkBuild(buildId: string, request: RelinkBuildRequest): Promise<ValidationReceipt> {
  return api<ValidationReceipt>(`/v1/builds/${buildId}/relink`, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
export const idempotencyKey = () => crypto.randomUUID();
