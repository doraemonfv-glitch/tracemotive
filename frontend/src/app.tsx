import { useCallback, useEffect, useState } from "react";
import { TraceDetail } from "./trace-detail";
import { TraceList } from "./trace-list";

const TRACE_ROUTE_PREFIX = "#/traces/";

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

export function TraceMotiveApp() {
  const [traceId, setTraceId] = useState<string | null>(() => traceIdFromLocation(window.location));

  useEffect(() => {
    const updateRoute = () => setTraceId(traceIdFromLocation(window.location));
    window.addEventListener("popstate", updateRoute);
    window.addEventListener("hashchange", updateRoute);
    return () => {
      window.removeEventListener("popstate", updateRoute);
      window.removeEventListener("hashchange", updateRoute);
    };
  }, []);

  const openTrace = useCallback((nextTraceId: string) => {
    window.history.pushState({}, "", traceRoute(nextTraceId));
    setTraceId(nextTraceId);
  }, []);

  const backToList = useCallback(() => {
    window.history.replaceState({}, "", "#/");
    setTraceId(null);
  }, []);

  if (traceId !== null) {
    return <TraceDetail key={traceId} traceId={traceId} onBack={backToList} />;
  }
  return <TraceList onOpenTrace={openTrace} />;
}
