import { defineConfig } from '@playwright/test'
export default defineConfig({ testDir:'./e2e', use:{baseURL:process.env.PLAYWRIGHT_BASE_URL||'http://localhost:8080'}, projects:[{name:'chromium',use:{browserName:'chromium'}},{name:'firefox',use:{browserName:'firefox'}},{name:'webkit',use:{browserName:'webkit'}}] })
