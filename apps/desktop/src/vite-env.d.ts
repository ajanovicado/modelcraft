/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_STUDIO_API_URL?: string;
  readonly VITE_STUDIO_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
