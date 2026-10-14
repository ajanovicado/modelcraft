import { apiRaw } from "./api";
import { createJudgementClient, type JudgementTransport } from "./judgement";

/** The judgement routes over the sidecar connection (PATCH and DELETE too, which the edit transport does not send). */
const transport: JudgementTransport = (method, path, body) => apiRaw(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const judgementClient = createJudgementClient(transport);
