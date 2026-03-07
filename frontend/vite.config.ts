import path from "path"
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3847,
    proxy: {
      "/health": "http://localhost:3848",
      "/projects": "http://localhost:3848",
      "/protocols": "http://localhost:3848",
      "/jobs": {
        target: "http://localhost:3848",
        // Disable response buffering so SSE (text/event-stream) works
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes) => {
            const ct = proxyRes.headers["content-type"] ?? ""
            if (ct.includes("text/event-stream")) {
              // Flush immediately for SSE
              proxyRes.headers["cache-control"] = "no-cache"
              proxyRes.headers["x-accel-buffering"] = "no"
            }
          })
        },
      },
      "/review": "http://localhost:3848",
      "/audit": "http://localhost:3848",
      "/dashboard": "http://localhost:3848",
      "/diagnostic": "http://localhost:3848",
      "/settings": "http://localhost:3848",
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
})
