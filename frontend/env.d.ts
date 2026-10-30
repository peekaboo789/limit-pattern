/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_USE_MOCK: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

interface Window {
  pywebview?: {
    api?: {
      save_file?: (filename: string, text: string) => unknown
      notify?: (title: string, message: string, key?: string) => unknown
    }
  }
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
