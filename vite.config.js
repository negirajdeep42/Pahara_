import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['icon.svg'],
      manifest: {
        name: 'PAHARA — Landslide Early Warning, Northeast India',
        short_name: 'PAHARA',
        description:
          'Predictive Analytics for Hazard Assessment and Rapid Alerts. Landslide early warning and risk monitoring across the eight states of Northeast India.',
        theme_color: '#070b10',
        background_color: '#070b10',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        scope: '/',
        icons: [
          { src: '/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' },
          { src: '/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'maskable' },
        ],
      },
      workbox: {
        // The app shell plus the risk grid itself: the dashboard has to open
        // and stay useful when the network is gone, which is the normal state
        // during a landslide event.
        globPatterns: [
          '**/*.{js,css,html,svg,woff2}',
          'risk_grid.geojson',
          // search is a primary interaction now, and the app hides the bar
          // entirely if this file is missing - so it has to survive offline
          'places.json',
        ],
        // The grid is several megabytes; the default 2 MiB precache ceiling
        // would silently skip exactly the file we most need offline.
        maximumFileSizeToCacheInBytes: 16 * 1024 * 1024,
        navigateFallback: '/index.html',
        cleanupOutdatedCaches: true,
        runtimeCaching: [
          {
            // Basemap tiles: whatever has been seen stays available offline.
            urlPattern: /^https:\/\/tiles\.openfreemap\.org\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'openfreemap-tiles',
              expiration: { maxEntries: 600, maxAgeSeconds: 60 * 60 * 24 * 30 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          {
            // Rainfall is only ever useful fresh, but a stale figure beats a
            // blank panel when the link drops.
            urlPattern: /^https:\/\/api\.open-meteo\.com\/.*/i,
            handler: 'NetworkFirst',
            options: {
              cacheName: 'open-meteo',
              networkTimeoutSeconds: 8,
              expiration: { maxEntries: 16, maxAgeSeconds: 60 * 60 * 6 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
          {
            urlPattern: /^https:\/\/fonts\.(googleapis|gstatic)\.com\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'google-fonts',
              expiration: { maxEntries: 24, maxAgeSeconds: 60 * 60 * 24 * 365 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
        ],
      },
    }),
  ],
  build: {
    chunkSizeWarningLimit: 1200,
  },
})
