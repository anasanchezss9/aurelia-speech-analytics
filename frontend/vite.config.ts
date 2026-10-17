import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Em dev, fazemos proxy de /api e /ws para o backend FastAPI em :8000
// Em produção (Databricks App), o backend serve o build estático.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
