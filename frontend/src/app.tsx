import { useCallback, useEffect, useState } from "react";
import { TraceComparison } from "./trace-comparison";
import { TraceDetail } from "./trace-detail";
import { TraceList } from "./trace-list";

const TRACE_ROUTE_PREFIX = "#/traces/";
const COMPARISON_ROUTE_PREFIX = "#/compare/";

export function traceRoute(traceId: string): string {
  return `${TRACE_ROUTE_PREFIX}${encodeURIComponent(traceId)}`;
}

export function traceIdFromLocation(location: Pick<Location, "hash">): string | null {
  if (!location.hash.startsWith(TRACE_ROUTE_PREFIX)) {
    return null;
  }
  const encodedTraceId = location.hash.slice(TRACE_ROUTE_PREFIX.length);
  if (encodedTraceId.length === 0) {
    return null;
  }
  try {
    const traceId = decodeURIComponent(encodedTraceId);
    return traceId.length === 0 ? null : traceId;
  } catch {
    return null;
  }
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
  try {
    const leftTraceId = decodeURIComponent(encodedIds[0]);
    const rightTraceId = decodeURIComponent(encodedIds[1]);
    return leftTraceId.length === 0 || rightTraceId.length === 0 ? null : { leftTraceId, rightTraceId };
  } catch {
    return null;
  }
}

type AppRoute =
  | { kind: "list" }
  | { kind: "trace"; traceId: string }
  | { kind: "comparison"; leftTraceId: string; rightTraceId: string };

function routeFromLocation(location: Pick<Location, "hash">): AppRoute {
  const comparison = comparisonIdsFromLocation(location);
  if (comparison !== null) {
    return { kind: "comparison", ...comparison };
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

  const openComparison = useCallback((leftTraceId: string, rightTraceId: string) => {
    window.history.pushState({}, "", comparisonRoute(leftTraceId, rightTraceId));
    setRoute({ kind: "comparison", leftTraceId, rightTraceId });
  }, []);

  const backToList = useCallback(() => {
    window.history.replaceState({}, "", "#/");
    setRoute({ kind: "list" });
  }, []);

  if (route.kind === "comparison") {
    return <TraceComparison key={`${route.leftTraceId}:${route.rightTraceId}`} leftTraceId={route.leftTraceId} rightTraceId={route.rightTraceId} onBack={backToList} />;
  }
  if (route.kind === "trace") {
    return <TraceDetail key={route.traceId} traceId={route.traceId} onBack={backToList} />;
  }
  return <TraceList onOpenTrace={openTrace} onStartComparison={openComparison} />;
}
