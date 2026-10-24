import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Root } from "./Root";
import "./styles.css";
import "./workspace.css";
import "./redesign.css";
import "./office.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Missing #root element.");
}
createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
