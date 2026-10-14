import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

async function loadApi({
  dev = false,
  invoke,
  fetch,
  token = "studio-development-token-local-only",
  apiUrl,
  omitToken = false,
}) {
  const compiled = await build({
    entryPoints: [fileURLToPath(new URL("../src/api.ts", import.meta.url))],
    bundle: true, platform: "node", format: "cjs", packages: "external", write: false,
    define: {
      "import.meta.env.DEV": String(dev),
      "import.meta.env.VITE_STUDIO_API_URL": apiUrl === undefined ? "undefined" : JSON.stringify(apiUrl),
      "import.meta.env.VITE_STUDIO_TOKEN": omitToken || token === undefined ? "undefined" : JSON.stringify(token),
    },
  });
  const loaded = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function("require", "module", "exports", "fetch", compiled.outputFiles[0].text)(
    (id) => id === "@tauri-apps/api/core" ? { invoke } : require(id),
    loaded, loaded.exports, fetch,
  );
  return loaded.exports;
}

const local = { endpoint: "http://127.0.0.1:23456", token: "test-token-at-least-sixteen" };

test("chat forwards chart context and consumes a filter action without a draft event", async () => {
  const action = { kind: "visual_date_filter", page_id: "overview", visual_id: "sales_trend" };
  let body;
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async (_url, init) => {
    body = JSON.parse(init.body);
    return new Response(`event: report_action\ndata: ${JSON.stringify(action)}\n\nevent: token\ndata: {"text":"Choose dates"}\n\nevent: done\ndata: {}\n\n`);
  } });
  const actions = []; let text = "";
  const result = await streamChat("workspace-test", "add date filter only for this chart", token => { text += token; }, new AbortController().signal,
    { idempotency_key: "date-command", expected_base_hash: "hash", context: { page_id: "overview", visual_id: "sales_trend" }, onReportAction: value => actions.push(value) });
  assert.deepEqual(actions, [action]);
  assert.deepEqual(body.context, { page_id: "overview", visual_id: "sales_trend" });
  assert.equal(result, undefined);
  assert.equal(text, "Choose dates");
});

test("category preview search and typed inspection do not rewrite DAX", async () => {
  const calls = [];
  const { evaluateDax } = await loadApi({ invoke: async () => local, fetch: async (_url, init) => { calls.push(JSON.parse(init.body)); return { ok: true, json: async () => ({ rows: [] }) }; } });
  const expression = "SUM('Orders'[Sales])";
  const preview = {column_id:"customer_name",search:"00123"};
  await evaluateDax("workspace-test", expression, 100, preview);
  for (const value of [null, false, 0, "00123"]) await evaluateDax("workspace-test", expression, 50, preview, undefined, undefined, {value});
  assert.ok(calls.every(call => call.expression === expression && call.preview.search === "00123"));
  assert.deepEqual(calls.slice(1).map(call => call.detail_group.value), [null, false, 0, "00123"]);
});

test("date preview context is separate from the original expression", async () => {
  const calls = [];
  const { evaluateDax } = await loadApi({ invoke: async () => local, fetch: async (_url, init) => { calls.push(JSON.parse(init.body)); return { ok: true, json: async () => ({ rows: [] }) }; } });
  const expression = "DIVIDE(SUM('Orders'[Profit]), SUM('Orders'[Sales]))";
  await evaluateDax("workspace-test", expression);
  await evaluateDax("workspace-test", expression, 100, {column_id: "order_date", grain: "month"});
  await evaluateDax("workspace-test", expression, 50, {column_id: "order_date", grain: "month"}, "2024-01-01");
  assert.ok(calls.every(call => call.expression === expression));
  assert.equal(calls[0].preview, undefined);
  assert.deepEqual(calls[1].preview, {column_id: "order_date", grain: "month"});
  assert.equal(calls[2].detail_period, "2024-01-01");
});

test("production never falls back to the development endpoint", async () => {
  let requests = 0;
  const { api } = await loadApi({ invoke: async () => { throw Error("offline"); }, fetch: () => { requests++; } });
  await assert.rejects(api("/v1/ping"), /No development connection was attempted/);
  assert.equal(requests, 0);
});

test("engine startup failure can recover on an explicit retry", async () => {
  let attempts = 0;
  const { api } = await loadApi({
    invoke: async () => { if (++attempts === 1) throw Error("starting"); return local; },
    fetch: async (url, init) => {
      assert.equal(url, `${local.endpoint}/v1/ping`);
      assert.equal(init.headers.Authorization, `Bearer ${local.token}`);
      return Response.json({ ready: true });
    },
  });
  await assert.rejects(api("/v1/ping"));
  assert.deepEqual(await api("/v1/ping"), { ready: true });
  assert.equal(attempts, 2);
});

test("only development builds allow the explicit browser development connection", async () => {
  const { api } = await loadApi({
    dev: true,
    invoke: async () => { throw Error("browser"); },
    fetch: async (url) => { assert.equal(url, "http://127.0.0.1:8765/v1/ping"); return Response.json({ ready: true }); },
  });
  assert.deepEqual(await api("/v1/ping"), { ready: true });
});

test("development browser fallback requires an explicit token", async () => {
  const { api } = await loadApi({
    dev: true,
    omitToken: true,
    invoke: async () => { throw Error("browser"); },
    fetch: () => assert.fail("must not transmit without a token"),
  });
  await assert.rejects(api("/v1/ping"), /VITE_STUDIO_TOKEN/);
});

test("engine paths outside /v1/ are rejected", async () => {
  const { api } = await loadApi({
    invoke: async () => local,
    fetch: () => assert.fail("must not transmit invalid path"),
  });
  await assert.rejects(api("/health"), /\/v1\//);
});

for (const endpoint of ["https://example.com", "http://192.168.1.1", "http://localhost", "http://127.0.0.1@evil.test", "http://127.0.0.1/api", "http://127.0.0.1?token=x"]) {
  test(`reject non-loopback or malformed engine connection: ${endpoint}`, async () => {
    const { api } = await loadApi({ invoke: async () => ({ ...local, endpoint }), fetch: () => assert.fail("must not transmit token") });
    await assert.rejects(api("/v1/ping"), /Only authenticated loopback/);
  });
}

test("mutations are not automatically retried after a network failure", async () => {
  let requests = 0;
  const { api } = await loadApi({ invoke: async () => local, fetch: async () => { requests++; throw Error("lost"); } });
  await assert.rejects(api("/v1/builds", { method: "POST", body: "{}" }), /Check the connection and retry/);
  assert.equal(requests, 1);
});

test("a truncated stream is an error, not a completed answer", async () => {
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async () => new Response('event: token\ndata: {"text":"partial"}\n\n') });
  let text = "";
  await assert.rejects(streamChat("workspace", "hello", (token) => { text += token; }, new AbortController().signal), /ended before completion/);
  assert.equal(text, "partial");
});

test("shared chat transmits only typed context and reports the saved draft", async () => {
  const context = { page_id: "detail", visual_id: "sales_chart" };
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async (_, init) => {
    const body = JSON.parse(init.body);
    assert.deepEqual(body.context, context);
    assert.equal(body.expected_base_hash, "a".repeat(64));
    assert.equal(body.message, "Add a trend to this page");
    return new Response('event: draft\ndata: {"draft_id":"saved-draft"}\n\nevent: done\ndata: {}\n\n');
  } });
  const result = await streamChat("workspace", "Add a trend to this page", () => {}, new AbortController().signal,
    { context, idempotency_key: "request-one", expected_base_hash: "a".repeat(64) });
  assert.equal(result, "saved-draft");
});

test("DAX advice carries a separate create action, never masquerading as a formula", async () => {
  let requestId;
  const { streamDaxAssistant } = await loadApi({ invoke: async () => local, fetch: async (_, init) => {
    assert.equal(JSON.parse(init.body).mode, "auto");
    requestId = JSON.parse(init.body).request_id;
    return new Response('event: assistant\ndata: {"kind":"advice","calculation_request":"Calculate average Sales"}\n\nevent: token\ndata: {"text":"Averages divide the sum by the count."}\n\nevent: done\ndata: {}\n\n');
  } });
  assert.deepEqual(await streamDaxAssistant("w", "s", { goal: "How do averages work?", expression: "old", mode: "auto" }, new AbortController().signal, () => {}), {
    text: "Averages divide the sum by the count.", kind: "advice", calculationRequest: "Calculate average Sales",
    requestId,
  });
});

test("unclassified or truncated DAX output cannot replace the editor", async () => {
  for (const output of ['event: token\ndata: {"text":"code"}\n\nevent: done\ndata: {}\n\n', 'event: assistant\ndata: {"kind":"formula"}\n\nevent: token\ndata: {"text":"code"}\n\n']) {
    const { streamDaxAssistant } = await loadApi({ invoke: async () => local, fetch: async () => new Response(output) });
    await assert.rejects(streamDaxAssistant("w", "s", { goal: "Create a total", expression: "old", mode: "auto" }, new AbortController().signal, () => {}));
  }
});

test("main and DAX requests transmit captured New chat boundaries with the API's snake-case field", async () => {
  const bodies = [];
  const { streamChat, streamDaxAssistant } = await loadApi({ invoke: async () => local, fetch: async (url, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(`${url.endsWith("/dax/assist") ? 'event: assistant\ndata: {"kind":"advice"}\n\n' : ""}event: done\ndata: {}\n\n`);
  } });
  for (const boundary of [0, 42]) await streamChat("workspace", "Explain this page", () => {}, new AbortController().signal, { idempotency_key: `request-main-${boundary}`, expected_base_hash: "a".repeat(64), chat_boundary: boundary });
  for (const boundary of [0, 7]) await streamDaxAssistant("workspace", "snapshot", { goal: "Explain this measure", expression: "1", mode: "explain", requestId: `request-dax-${boundary}`, chatBoundary: boundary }, new AbortController().signal, () => {});
  assert.deepEqual(bodies.map(body => body.chat_boundary), [0, 42, 0, 7]);
  assert.ok(bodies.every(body => !Object.hasOwn(body, "chatBoundary")));
});

test("already-cancelled requests do not resolve a connection or send anything", async () => {
  const controller = new AbortController();
  controller.abort();
  const never = () => assert.fail("cancelled request must not start");
  const { api, apiRaw, streamChat } = await loadApi({ invoke: never, fetch: never });
  await assert.rejects(api("/v1/ping", { signal: controller.signal }), { name: "AbortError" });
  await assert.rejects(apiRaw("/v1/ping", { signal: controller.signal }), { name: "AbortError" });
  await assert.rejects(streamChat("workspace", "Question", never, controller.signal), { name: "AbortError" });
});

test("cancellation during connection resolution prevents the later assistant request", async () => {
  let resolve;
  const connected = new Promise(done => { resolve = done; });
  const controller = new AbortController();
  const { streamChat } = await loadApi({ invoke: () => connected, fetch: () => assert.fail("cancelled startup must not fetch") });
  const response = streamChat("workspace", "Question", () => {}, controller.signal);
  controller.abort();
  resolve(local);
  await assert.rejects(response, { name: "AbortError" });
});

test("aborted GET requests preserve AbortError and are never automatically retried", async () => {
  for (const name of ["api", "apiRaw"]) {
    let requests = 0;
    const controller = new AbortController();
    const failure = new DOMException("Cancelled request", "AbortError");
    const client = await loadApi({ invoke: async () => local, fetch: async () => { requests++; controller.abort(); throw failure; } });
    await assert.rejects(client[name]("/v1/ping", { signal: controller.signal }), error => error === failure);
    assert.equal(requests, 1);
  }
});

test("cancellation during JSON parsing cannot commit a late read result", async () => {
  for (const name of ["api", "apiRaw"]) {
    const controller = new AbortController();
    const client = await loadApi({ invoke: async () => local, fetch: async () => ({ ok: true, status: 200, json: async () => { controller.abort(); return { stale: true }; } }) });
    await assert.rejects(client[name]("/v1/ping", { signal: controller.signal }), { name: "AbortError" });
  }
});

test("Stop discards remaining buffered tokens, statuses and actions immediately", async () => {
  const controller = new AbortController();
  const tokens = [], statuses = [], actions = [], edits = [];
  const wire = 'event: token\ndata: {"text":"First"}\n\nevent: token\ndata: {"text":"Stale"}\n\nevent: status\ndata: {"text":"Writing"}\n\nevent: report_action\ndata: {"kind":"visual_date_filter"}\n\nevent: edit_progress\ndata: {"stage":"writing","label":"Late write"}\n\nevent: draft\ndata: {"draft_id":"stale-draft"}\n\nevent: done\ndata: {}\n\n';
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async () => new Response(wire) });
  await assert.rejects(streamChat("workspace", "Question", token => { tokens.push(token); controller.abort(); }, controller.signal, { idempotency_key: "request-stop", expected_base_hash: "a".repeat(64), onStatus: value => statuses.push(value), onReportAction: value => actions.push(value), onEditEvent: value => edits.push(value) }), { name: "AbortError" });
  assert.deepEqual(tokens, ["First"]);
  assert.deepEqual(statuses, []);
  assert.deepEqual(actions, []);
  assert.deepEqual(edits, []);
});

test("a terminal done receipt completes without waiting for EOF and cancels the reader", async () => {
  let cancelled = false;
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('event: draft\ndata: {"draft_id":"saved"}\n\nevent: done\ndata: {}\n\nevent: token\ndata: {"text":"after done"}\n\nevent: report_action\ndata: {"kind":"visual_date_filter"}\n\nevent: edit_progress\ndata: {"stage":"writing","label":"Late write"}\n\n')); }, cancel() { cancelled = true; } });
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async () => new Response(stream) });
  const late = () => assert.fail("terminal events stop callbacks");
  assert.equal(await streamChat("workspace", "Question", late, new AbortController().signal, { idempotency_key: "request-completed", expected_base_hash: "a".repeat(64), onReportAction: late, onEditEvent: late }), "saved");
  assert.equal(cancelled, true);
});

test("standard CRLF event boundaries survive arbitrary network chunks", async () => {
  const parts = ['event: token\r\ndata: {"text":"Answer"}\r', '\n\r', '\nevent: done\r\ndata: {}\r\n\r', '\n'];
  const stream = new ReadableStream({ start(controller) { for (const part of parts) controller.enqueue(new TextEncoder().encode(part)); controller.close(); } });
  const { streamChat } = await loadApi({ invoke: async () => local, fetch: async () => new Response(stream) });
  let answer = "";
  await streamChat("workspace", "Question", token => { answer += token; }, new AbortController().signal);
  assert.equal(answer, "Answer");
});
