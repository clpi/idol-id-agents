import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { registerFleetSnapshot } from "./snapshot.js";


export default definePluginEntry({
  id: "openclaw-fleet-snapshot",
  name: "IDOL Fleet Active Work Snapshot",
  description: "Authenticated read-only projection of canonical gateway active-work counters.",
  register(api) {
    registerFleetSnapshot(api, { sdkDefinePluginEntry: definePluginEntry });
  },
});
