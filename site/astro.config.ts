import { defineConfig } from "astro/config";

export default defineConfig({
  build: {
    inlineStylesheets: "always",
  },
  devToolbar: {
    enabled: false,
  },
  outDir: "./build",
  output: "static",
  vite: {
    cacheDir: ".astro/vite",
  },
});
