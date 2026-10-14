import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({root:'mvp',plugins:[react()],build:{outDir:'../mvp-dist',emptyOutDir:true},
  server:{host:'127.0.0.1',port:5174,proxy:{'/api':'http://127.0.0.1:8767'}}});
