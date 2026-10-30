import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  base: '/hokm/',
  build: {
    outDir: path.resolve(__dirname, '../static/hokm'),
    emptyOutDir: true,
  },
});
