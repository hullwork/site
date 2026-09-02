import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Products are served by sites-api under `/console/` (Contract §4.5), not the site root, so
// base must carry this section - otherwise the typed /assets/... will hit the 404 of sites-api.
//
// dev connects directly to the sites-api NodePort exposed by the local kubeadm cluster. admin token is entered from the **login page**
// The sessionStorage and dev header are only used to avoid login debugging; they are not injected by default.
// See the 401 on the spot instead of thinking "panel is broken".
const apiTarget = process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:18091";
const devAdminToken = process.env.VITE_DEV_ADMIN_TOKEN ?? "";

export default defineConfig({
  base: "/console/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 3200,
    proxy: {
      "/v1": {
        target: apiTarget,
        changeOrigin: true,
        headers: devAdminToken
          ? { "X-Sites-Service-Token": devAdminToken }
          : undefined,
      },
    },
  },
});
