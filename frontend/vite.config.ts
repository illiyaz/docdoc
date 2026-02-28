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
      "/jobs": "http://localhost:3848",
      "/review": "http://localhost:3848",
      "/audit": "http://localhost:3848",
      "/diagnostic": "http://localhost:3848",
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
})
