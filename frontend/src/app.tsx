import { useCallback, useEffect, useState } from "react";
import { TraceComparison } from "./trace-comparison";
import { TraceDetail } from "./trace-detail";
import { TraceList } from "./trace-list";

const TRACE_ROUTE_PREFIX = "#/traces/";
const COMPARISON_ROUTE_PREFIX = "#/compare/";
const SPAN_ROUTE_MARKER = "spans";

const TRACE_ID_PATTERN = /^[0-9a-f]{32}$/;
const SPAN_ID_PATTERN = /^[0-9a-f]{16}$/;

function decodeRouteId(value: string, pattern: RegExp): string | null {
  try {
    const decoded = decodeURIComponent(value);
    return pattern.test(decoded) ? decoded : null;
  } catch {
    return null;
  }
}

export function traceRoute(traceId: string): string {
  return `${TRACE_ROUTE_PREFIX}${encodeURIComponent(traceId)}`;
}

export function traceIdFromLocation(location: Pick<Location, "hash">): string | null {
  if (!location.hash.startsWith(TRACE_ROUTE_PREFIX)) {
    return null;
  }
  const segments = location.hash.slice(TRACE_ROUTE_PREFIX.length).split("/");
  if (segments.length !== 1 || segments[0].length === 0) {
    return null;
  }
  return decodeRouteId(segments[0], TRACE_ID_PATTERN);
}

export function spanRoute(traceId: string, spanId: string): string {
  return `${TRACE_ROUTE_PREFIX}${encodeURIComponent(traceId)}/${SPAN_ROUTE_MARKER}/${encodeURIComponent(spanId)}`;
}

export function spanIdsFromLocation(location: Pick<Location, "hash">): { traceId: string; spanId: string } | null {
  if (!location.hash.startsWith(TRACE_ROUTE_PREFIX)) {
    return null;
  }
  const segments = location.hash.slice(TRACE_ROUTE_PREFIX.length).split("/");
  if (segments.length !== 3 || segments[1] !== SPAN_ROUTE_MARKER || segments[0].length === 0 || segments[2].length === 0) {
    return null;
  }
  const traceId = decodeRouteId(segments[0], TRACE_ID_PATTERN);
  const spanId = decodeRouteId(segments[2], SPAN_ID_PATTERN);
  return traceId === null || spanId === null ? null : { traceId, spanId };
}

export function comparisonRoute(leftTraceId: string, rightTraceId: string): string {
  return `${COMPARISON_ROUTE_PREFIX}${encodeURIComponent(leftTraceId)}/${encodeURIComponent(rightTraceId)}`;
}

export function comparisonIdsFromLocation(location: Pick<Location, "hash">): { leftTraceId: string; rightTraceId: string } | null {
  if (!location.hash.startsWith(COMPARISON_ROUTE_PREFIX)) {
    return null;
  }
  const encodedIds = location.hash.slice(COMPARISON_ROUTE_PREFIX.length).split("/");
  if (encodedIds.length !== 2 || encodedIds.some((value) => value.length === 0)) {
    return null;
  }
  const leftTraceId = decodeRouteId(encodedIds[0], TRACE_ID_PATTERN);
  const rightTraceId = decodeRouteId(encodedIds[1], TRACE_ID_PATTERN);
  return leftTraceId === null || rightTraceId === null ? null : { leftTraceId, rightTraceId };
}

type AppRoute =
  | { kind: "list" }
  | { kind: "trace"; traceId: string }
  | { kind: "span"; traceId: string; spanId: string; returnTo: string | null }
  | { kind: "comparison"; leftTraceId: string; rightTraceId: string };

function routeFromLocation(location: Pick<Location, "hash">): AppRoute {
  const comparison = comparisonIdsFromLocation(location);
  if (comparison !== null) {
    return { kind: "comparison", ...comparison };
  }
  const span = spanIdsFromLocation(location);
  if (span !== null) {
    return { kind: "span", ...span, returnTo: null };
  }
  const traceId = traceIdFromLocation(location);
  return traceId === null ? { kind: "list" } : { kind: "trace", traceId };
}

export function TraceMotiveApp() {
  const [route, setRoute] = useState<AppRoute>(() => routeFromLocation(window.location));

  useEffect(() => {
    const updateRoute = () => setRoute(routeFromLocation(window.location));
    window.addEventListener("popstate", updateRoute);
    window.addEventListener("hashchange", updateRoute);
    return () => {
      window.removeEventListener("popstate", updateRoute);
      window.removeEventListener("hashchange", updateRoute);
    };
  }, []);

  const openTrace = useCallback((nextTraceId: string) => {
    window.history.pushState({}, "", traceRoute(nextTraceId));
    setRoute({ kind: "trace", traceId: nextTraceId });
  }, []);

  const openSpan = useCallback((nextTraceId: string, nextSpanId: string) => {
    const returnTo = route.kind === "comparison" ? comparisonRoute(route.leftTraceId, route.rightTraceId) : null;
    window.history.pushState({}, "", spanRoute(nextTraceId, nextSpanId));
    setRoute({ kind: "span", traceId: nextTraceId, spanId: nextSpanId, returnTo });
  }, [route]);

  const openComparison = useCallback((leftTraceId: string, rightTraceId: string) => {
    window.history.pushState({}, "", comparisonRoute(leftTraceId, rightTraceId));
    setRoute({ kind: "comparison", leftTraceId, rightTraceId });
  }, []);

  const backToList = useCallback(() => {
    window.history.replaceState({}, "", "#/");
    setRoute({ kind: "list" });
  }, []);

  if (route.kind === "comparison") {
    return <TraceComparison key={`${route.leftTraceId}:${route.rightTraceId}`} leftTraceId={route.leftTraceId} rightTraceId={route.rightTraceId} onBack={backToList} onOpenSpan={openSpan} />;
  }
  if (route.kind === "span") {
    const back = route.returnTo === null
      ? backToList
      : () => {
        window.history.replaceState({}, "", route.returnTo!);
        setRoute(routeFromLocation(window.location));
      };
    return <TraceDetail key={`${route.traceId}:${route.spanId}`} traceId={route.traceId} spanId={route.spanId} onBack={back} backLabel={route.returnTo === null ? "Back to traces" : "Back to comparison"} />;
  }
  if (route.kind === "trace") {
    return <TraceDetail key={route.traceId} traceId={route.traceId} onBack={backToList} />;
  }
  return <TraceList onOpenTrace={openTrace} onStartComparison={openComparison} />;
}
