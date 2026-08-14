import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The collector is local-only in v0.1.  Keeping the browser request relative
// lets the development proxy retain that boundary without enabling CORS.
export default defineConfig({
  plugins: [react()],
  // dist/ is disposable staging output. The release-side package:ui step
  // copies its production output into the Python package.
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    host: "127.0.0.1",
    strictPort: true,
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
