import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],

  // Read the repo-root .env so the whole project has one config file.
  // Only VITE_-prefixed vars are exposed to the client.
  envDir: "../../",

  server: {
    port: 5173,
    /**
     * Proxy /api to the API in development.
     *
     * This is why no component contains a hostname: the browser talks to its own origin,
     * so there is no cross-origin request and no CORS preflight in dev. Session cookies
     * are first-party too, which avoids the SameSite=Strict pitfalls entirely.
     */
    proxy: {
      "/api": {
        target: "http://localhost:5000",
        changeOrigin: true,
      },
    },
  },

  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
