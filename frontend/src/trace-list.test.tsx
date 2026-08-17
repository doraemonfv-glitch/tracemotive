import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { decodeTraceListResponse, TRACE_LIST_PATH } from "./api";
import { EmptyStateOnboarding, ONBOARDING_COMMANDS } from "./onboarding";
import { TraceList } from "./trace-list";
import type { TraceListResponse, TraceSummary } from "./types";

const alpha: TraceSummary = {
  trace_id: "00000000-0000-4000-8000-000000000001",
  name: "Alpha workflow",
  started_at: "2026-08-10T13:00:02.000000Z",
  ended_at: "2026-08-10T13:00:03.250000Z",
  status: "ok",
  latency_ms: 1250,
  span_count: 4n,
  error_count: 2n,
  llm_call_count: 2n,
  input_tokens: 0n,
  output_tokens: null,
};

const beta: TraceSummary = {
  ...alpha,
  trace_id: "00000000-0000-4000-8000-000000000002",
  name: "Beta workflow",
  status: "unset",
  ended_at: null,
  latency_ms: null,
  input_tokens: null,
  output_tokens: 0n,
};

const gamma: TraceSummary = {
  ...alpha,
  trace_id: "00000000-0000-4000-8000-000000000003",
  name: "Gamma workflow",
  status: "error",
};

function wireValue(value: unknown): unknown {
  if (typeof value === "bigint") {
    return Number(value);
  }
  if (Array.isArray(value)) {
    return value.map(wireValue);
  }
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, wireValue(entry)]));
  }
  return value;
}

function response(payload: TraceListResponse, status = 200): Response {
  return new Response(JSON.stringify(wireValue(payload)), { status, headers: { "Content-Type": "application/json" } });
}

function page(items: TraceSummary[], offset = 0n, total = BigInt(items.length)): TraceListResponse {
  return { items, limit: 50, offset, total };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("TraceList", () => {
  it("renders the ordered TraceSummary fields and keeps status independent from child errors", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(page([alpha, beta, gamma])));
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);

    await screen.findByText("Alpha workflow");
    expect(screen.getAllByRole("row").map((row) => row.textContent)).toEqual([
      expect.stringContaining("Trace"),
      expect.stringContaining("Alpha workflow"),
      expect.stringContaining("Beta workflow"),
      expect.stringContaining("Gamma workflow"),
    ]);
    expect(screen.getAllByText("OK").some((node) => node.classList.contains("status-ok"))).toBe(true);
    expect(screen.getAllByText("Unset").some((node) => node.classList.contains("status-unset"))).toBe(true);
    expect(screen.getAllByText("Error").some((node) => node.classList.contains("status-error"))).toBe(true);
    expect(screen.getAllByText("2026-08-10T13:00:03.250000Z").length).toBe(2);
    expect(screen.getByText("Not ended")).toBeTruthy();
    expect(screen.getAllByText("1,250 ms").length).toBe(2);
    expect(screen.getByText("Unavailable")).toBeTruthy();
    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("Unknown").length).toBeGreaterThanOrEqual(2);
    expect(fetchMock).toHaveBeenCalledWith(
      `${TRACE_LIST_PATH}?limit=50&offset=0`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("shows the first-run onboarding state with exact local commands and product limits", async () => {
    const first = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValue(first.promise);
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);
    expect(screen.getByText("Loading trace list...")).toBeTruthy();

    first.resolve(response(page([])));
    expect(await screen.findByRole("heading", { name: "See what changed in an AI agent run" })).toBeTruthy();
    expect(screen.getByText("TraceMotive compares AI agent executions and identifies the first behavioral divergence supported by the available evidence.")).toBeTruthy();
    expect(screen.getByText(/It does not claim that an observed divergence caused a failure/)).toBeTruthy();
    expect(screen.getByText("tracemotive demo")).toBeTruthy();
    expect(screen.getByText("tracemotive serve")).toBeTruthy();
    expect(screen.getByText("tracemotive demo --scenario uncertain")).toBeTruthy();
    expect(screen.getByText('python -m pip install "tracemotive[openai-agents]"')).toBeTruthy();
    expect(screen.getByText(/Generic Python is manual instrumentation/)).toBeTruthy();
    expect(screen.getByText(/LangGraph is not currently supported/)).toBeTruthy();
    expect(screen.getByText(/Normal installed users do not need Node.js, npm, or a repository checkout/)).toBeTruthy();
    expect(screen.queryByText("python -m examples.openai_agents_example")).toBeNull();
    expect(screen.queryByText(/pip install -e/)).toBeNull();
    expect(screen.getByText(/TraceMotive itself needs no API key/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(`${TRACE_LIST_PATH}?limit=50&offset=0`, expect.anything());
  });

  it("copies the deterministic demo command without executing or interpolating it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(page([]))));

    render(<TraceList />);

    await screen.findByText("tracemotive demo");
    fireEvent.click(screen.getByRole("button", { name: "Copy Run the identified example" }));
    expect(writeText).toHaveBeenCalledWith("tracemotive demo");
    expect(await screen.findByText("Copied.")).toBeTruthy();
  });

  it("keeps onboarding commands static when hostile trace text is present", () => {
    const hostile = "<script>window.location='https://evil.invalid'</script>";
    const { container } = render(<><span>{hostile}</span><EmptyStateOnboarding /></>);

    expect(screen.getByText(ONBOARDING_COMMANDS.identifiedDemo)).toBeTruthy();
    expect(screen.getByText(hostile)).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain(ONBOARDING_COMMANDS.identifiedDemo);
  });

  it("leaves a focused selectable command when clipboard access is denied", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(page([]))));

    render(<TraceList />);

    await screen.findByText("tracemotive demo");
    fireEvent.click(screen.getByRole("button", { name: "Copy Run the identified example" }));
    expect(await screen.findByText("Clipboard unavailable. Select the command above.")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByLabelText("Run the identified example command"));
  });

  it("transitions from onboarding to the normal trace UI without inventing a comparison", async () => {
    const empty = deferred<Response>();
    const filtered = deferred<Response>();
    const fetchMock = vi.fn((request: string) => request.includes("status=error") ? filtered.promise : empty.promise);
    const onStartComparison = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList onStartComparison={onStartComparison} />);
    empty.resolve(response(page([])));
    expect(await screen.findByText("tracemotive demo")).toBeTruthy();

    fireEvent.change(screen.getByRole("combobox", { name: "Status filter" }), { target: { value: "error" } });
    filtered.resolve(response(page([gamma], 0n, 1n)));
    expect(await screen.findByText("Gamma workflow")).toBeTruthy();
    expect(screen.queryByText("tracemotive demo")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Compare selected traces" }));
    expect(onStartComparison).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Compare selected traces" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("uses a controlled error message without exposing the response body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("C:\\secret\\database.db", { status: 500 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);

    expect(await screen.findByText("Unable to load traces.")).toBeTruthy();
    expect(screen.queryByText(/database\.db/)).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("sends the owned filters and resets pagination when either filter changes", async () => {
    const fetchMock = vi.fn((request: string) => Promise.resolve(response(page([alpha], 0n, 101n))));
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);
    await screen.findByText("Alpha workflow");

    fireEvent.change(screen.getByRole("combobox", { name: "Status filter" }), { target: { value: "error" } });
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${TRACE_LIST_PATH}?limit=50&offset=0&status=error`,
      expect.anything(),
    ));

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${TRACE_LIST_PATH}?limit=50&offset=50&status=error`,
      expect.anything(),
    ));

    fireEvent.change(screen.getByRole("searchbox", { name: "Trace name filter" }), { target: { value: "alpha" } });
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${TRACE_LIST_PATH}?limit=50&offset=0&status=error&name=alpha`,
      expect.anything(),
    ));

    fireEvent.change(screen.getByRole("searchbox", { name: "Trace name filter" }), { target: { value: "" } });
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${TRACE_LIST_PATH}?limit=50&offset=0&status=error&name=`,
      expect.anything(),
    ));
  });

  it("uses Query API pages directly and resets an out-of-range final page", async () => {
    const fetchMock = vi.fn((request: string) => {
      if (request.includes("offset=50")) {
        return Promise.resolve(response(page([], 50n, 50n)));
      }
      return Promise.resolve(response(page([alpha], 0n, 51n)));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);
    await screen.findByText("Alpha workflow");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getByText(/Page 1 of 2/)).toBeTruthy();
    expect(fetchMock.mock.calls.map(([request]) => request)).toEqual([
      `${TRACE_LIST_PATH}?limit=50&offset=0`,
      `${TRACE_LIST_PATH}?limit=50&offset=50`,
      `${TRACE_LIST_PATH}?limit=50&offset=0`,
    ]);
  });

  it("shows distinct Query API results when navigating between pages", async () => {
    const fetchMock = vi.fn((request: string) => {
      if (request.includes("offset=50")) {
        return Promise.resolve(response(page([beta], 50n, 51n)));
      }
      return Promise.resolve(response(page([alpha], 0n, 51n)));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);
    expect(await screen.findByText("Alpha workflow")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Beta workflow")).toBeTruthy();
    expect(screen.queryByText("Alpha workflow")).toBeNull();
    expect(screen.getByText(/Page 2 of 2/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("Alpha workflow")).toBeTruthy();
    expect(screen.queryByText("Beta workflow")).toBeNull();
  });

  it("does not allow a slower obsolete filter request to overwrite the current request", async () => {
    const alphaRequest = deferred<Response>();
    const betaRequest = deferred<Response>();
    const fetchMock = vi.fn((request: string) => {
      if (request.includes("name=alpha")) {
        return alphaRequest.promise;
      }
      if (request.includes("name=beta")) {
        return betaRequest.promise;
      }
      return Promise.resolve(response(page([gamma])));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<TraceList />);
    await screen.findByText("Gamma workflow");
    const filter = screen.getByRole("searchbox", { name: "Trace name filter" });
    fireEvent.change(filter, { target: { value: "alpha" } });
    fireEvent.change(filter, { target: { value: "beta" } });

    betaRequest.resolve(response(page([{ ...beta, name: "Current beta" }])));
    expect(await screen.findByText("Current beta")).toBeTruthy();
    alphaRequest.resolve(response(page([{ ...alpha, name: "Stale alpha" }])));
    await Promise.resolve();
    expect(screen.queryByText("Stale alpha")).toBeNull();
    expect(screen.getByText("Current beta")).toBeTruthy();
  });

  it("renders untrusted trace names as inert text", async () => {
    const hostileName = "<img src=x onerror=alert(1)><script>alert(1)</script>";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(page([{ ...alpha, name: hostileName }]))));

    render(<TraceList />);

    expect(await screen.findByText(hostileName)).toBeTruthy();
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("svg")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
  });

  it("preserves API microsecond latency precision without trailing noise", async () => {
    const cases: Array<[string, string]> = [
      ["0", "0 ms"],
      ["0.001", "0.001 ms"],
      ["0.010", "0.01 ms"],
      ["0.100", "0.1 ms"],
      ["1", "1 ms"],
      ["1.001", "1.001 ms"],
      ["null", "Unavailable"],
    ];
    const rawItems = cases.map(([latency, label], index) =>
      `{"trace_id":"latency-${index}","name":"Latency ${label}","started_at":"2026-08-10T13:00:00Z","ended_at":"2026-08-10T13:00:00Z","status":"ok","latency_ms":${latency},"span_count":4,"error_count":0,"llm_call_count":2,"input_tokens":0,"output_tokens":0}`,
    ).join(",");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(`{"items":[${rawItems}],"limit":50,"offset":0,"total":7}`)),
    );

    render(<TraceList />);

    for (const [, expected] of cases) {
      expect(await screen.findByText(expected)).toBeTruthy();
    }
  });

  it("losslessly decodes every token integer boundary before formatting", () => {
    const tokenLiterals = [
      "0",
      "1",
      "9007199254740991",
      "9007199254740992",
      "9007199254740993",
      "123456789012345678901234567890",
      "null",
    ];
    const rawItems = tokenLiterals.map((tokens, index) =>
      `{"trace_id":"trace-${index}","name":"Token ${index}","started_at":"2026-08-10T13:00:00Z","ended_at":"2026-08-10T13:00:00Z","status":"ok","latency_ms":0.001,"span_count":4,"error_count":0,"llm_call_count":2,"input_tokens":${tokens},"output_tokens":${tokens}}`,
    ).join(",");
    const decoded = decodeTraceListResponse(
      `{"items":[${rawItems}],"limit":50,"offset":0,"total":7}`,
    );

    expect(decoded.items.map((item) => item.input_tokens)).toEqual([
      0n,
      1n,
      9007199254740991n,
      9007199254740992n,
      9007199254740993n,
      123456789012345678901234567890n,
      null,
    ]);
    expect(decoded.limit).toBe(50);
    expect(decoded.offset).toBe(0n);
    expect(decoded.total).toBe(7n);
    expect(decoded.items[0].latency_ms).toBe(0.001);
    expect(decoded.items[0].span_count).toBe(4n);
  });

  it("renders an unsafe raw JSON token literal without rounding it", async () => {
    const rawResponse = `{"items":[{"trace_id":"trace-large-token","name":"Large token trace","started_at":"2026-08-10T13:00:00Z","ended_at":"2026-08-10T13:00:00Z","status":"ok","latency_ms":1,"span_count":4,"error_count":0,"llm_call_count":2,"input_tokens":9007199254740993,"output_tokens":9007199254740993}],"limit":50,"offset":0,"total":1}`;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(rawResponse, { headers: { "Content-Type": "application/json" } })),
    );

    render(<TraceList />);

    expect(await screen.findAllByText("9,007,199,254,740,993")).toHaveLength(2);
    expect(screen.queryByText("9,007,199,254,740,992")).toBeNull();
  });

  it("keeps malformed success payloads in the controlled error state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{", { status: 200 })));

    render(<TraceList />);

    expect(await screen.findByText("Unable to load traces.")).toBeTruthy();
  });
});
