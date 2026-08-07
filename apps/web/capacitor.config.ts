import type { CapacitorConfig } from "@capacitor/cli";

/** Collegium native shell: the SAME React build wrapped for
 *  iOS/Android. The app talks to the backend over the same https endpoints —
 *  no native-only APIs beyond push (web push → APNs/FCM via the Capacitor
 *  push plugin lands with the VM move, env-gated). */
const config: CapacitorConfig = {
  appId: "com.collegiumlabs.collegium",
  appName: "Collegium",
  webDir: "dist",
  server: {
    // Production loads the bundled shell; the API base is same-origin via the
    // deployed backend. Dev override: `cap sync --livereload` against vite.
    androidScheme: "https",
  },
  backgroundColor: "#0b0e11",
};

export default config;
