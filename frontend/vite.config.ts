import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The collector is local-only in v0.1.  Keeping the browser request relative
// lets the development proxy retain that boundary without enabling CORS.
export default defineConfig({
  plugins: [react()],
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
