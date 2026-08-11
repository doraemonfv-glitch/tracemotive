import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { TraceList } from "./trace-list";
import "./styles.css";

const rootElement = document.getElementById("root");

if (rootElement === null) {
  throw new Error("AgentLens root element is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <TraceList />
  </StrictMode>,
);
