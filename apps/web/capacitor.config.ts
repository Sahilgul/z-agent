import type { CapacitorConfig } from "@capacitor/cli";

/** Zagent native shell (plan Phase 5): the SAME React build wrapped for
 *  iOS/Android. The app talks to the Tailscale-tunneled backend over the same
 *  https endpoints — no native-only APIs beyond push (web push → APNs/FCM via
 *  Capacitor push plugin lands with the VM move, env-gated). */
const config: CapacitorConfig = {
  appId: "ai.zagentharness.zagent",
  appName: "zagent",
  webDir: "dist",
  server: {
    // Production loads the bundled shell; the API base is same-origin via the
    // deployed backend. Dev override: `cap sync --livereload` against vite.
    androidScheme: "https",
  },
  backgroundColor: "#0b0e11",
};

export default config;
