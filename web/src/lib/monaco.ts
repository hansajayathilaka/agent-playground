import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import agentSchema from "../gen/agent.schema.json";

// Bundle Monaco locally instead of loading it from a CDN.
self.MonacoEnvironment = {
  getWorker(_id: string, label: string) {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  },
};

monaco.languages.json.jsonDefaults.setDiagnosticsOptions({
  validate: true,
  allowComments: false,
  schemas: [
    {
      uri: "agent-playground://schemas/agent.json",
      fileMatch: ["agent.json"],
      schema: agentSchema,
    },
  ],
});

loader.config({ monaco });
