import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { ApplicationShell } from "./App";
import "./styles.css";
import "./v2.css";
import "./workbenchTheme.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ApplicationShell />
    </BrowserRouter>
  </React.StrictMode>,
);
