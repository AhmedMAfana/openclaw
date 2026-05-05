// Vite HMR config for a per-chat instance behind Cloudflare Tunnel.
//
// The browser reaches the dev server via wss://${INSTANCE_HMR_HOST}:443
// — Cloudflare terminates TLS at the edge, cloudflared forwards plain
// HTTP/WS to node:5173 inside the compose network.
//
// arch doc §5.4 + research.md §6: clientPort MUST be 443, protocol MUST
// be wss. allowedHosts must include both the main host and the HMR host
// so a browser already on INSTANCE_HOST can bootstrap HMR to
// INSTANCE_HMR_HOST without Vite refusing the origin.

import { defineConfig } from 'vite';
import laravel from 'laravel-vite-plugin';
import vue from '@vitejs/plugin-vue';

const host = process.env.INSTANCE_HOST;
const hmrHost = process.env.INSTANCE_HMR_HOST;

if (!host || !hmrHost) {
  throw new Error(
    'INSTANCE_HOST and INSTANCE_HMR_HOST must be set by the orchestrator.'
  );
}

export default defineConfig({
  plugins: [
    laravel({
      input: ['resources/css/app.css', 'resources/js/app.js'],
      refresh: true,
    }),
    vue(),
  ],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    hmr: {
      host: hmrHost,
      clientPort: 443,
      protocol: 'wss',
    },
    // Vite refuses requests whose Host header isn't in this list; both
    // the web origin and the hmr origin are valid approach paths.
    allowedHosts: [host, hmrHost],
  },
});
