import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { TraceMotiveApp } from "./app";
import "./styles.css";

const rootElement = document.getElementById("root");

if (rootElement === null) {
  throw new Error("TraceMotive root element is missing");
}

createRoot(rootElement).render(
  <StrictMode>
    <TraceMotiveApp />
  </StrictMode>,
);
