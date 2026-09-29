import { expect, test } from '@playwright/test'

const user = {id:'user-1',username:'alice'}
const readyTemplate = {id:'template-1',name:'Brand.pptx',status:'ready',slide_count:12,error:null,created_at:new Date().toISOString(),analysis_job:null}

test.beforeEach(async ({page}) => {
  await page.route('**/api/v1/auth/me', route => route.fulfill({json:user}))
})

test('shows an empty template library and upload control', async ({page}) => {
  await page.route('**/api/v1/templates', route => route.fulfill({json:{items:[]}}))
  await page.goto('/templates')
  await expect(page.getByText('Нет шаблонов')).toBeVisible()
  await expect(page.getByText('Загрузить PPTX')).toBeVisible()
})

test('uploads a template with contextual analysis settings', async ({page}) => {
  await page.route('**/api/v1/templates', async route => {
    if(route.request().method()==='GET')return route.fulfill({json:{items:[]}})
    const body=route.request().postData()||''
    expect(body).toContain('name="use_vlm"')
    expect(body).toContain('false')
    expect(body).toContain('name="catalog_workers"')
    expect(body).toContain('7')
    expect(body).toContain('name="enrichment_workers"')
    expect(body).toContain('6')
    return route.fulfill({status:202,json:{
      ...readyTemplate,id:'template-uploaded',name:'Uploaded.pptx',slide_count:8,
      analysis_job:{id:'analysis-1',status:'ready',stage:'ready',progress:100,error:null,created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:01:00Z'},
    }})
  })
  await page.goto('/templates')
  await page.getByRole('combobox',{name:'Потоки VLM'}).selectOption('6')
  await page.getByRole('switch',{name:'VLM-анализ'}).click()
  await expect(page.getByRole('combobox',{name:'Потоки VLM'})).toBeDisabled()
  await page.getByRole('combobox',{name:'Потоки каталога'}).selectOption('7')
  await page.locator('input[accept=".pptx"]').setInputFiles({name:'Uploaded.pptx',mimeType:'application/vnd.openxmlformats-officedocument.presentationml.presentation',buffer:Buffer.from('test')})
  await expect(page.getByText('8 слайдов').first()).toBeVisible()
  await expect(page.getByLabel('Прошло 01:00')).toBeVisible()
})

test('deletes the last ready template from the library', async ({page}) => {
  let deleted=false
  await page.route('**/api/v1/templates/template-1', route => {
    if(route.request().method()==='DELETE'){
      deleted=true
      return route.fulfill({status:204,body:''})
    }
    return route.fulfill({json:readyTemplate})
  })
  await page.route('**/api/v1/templates', route => route.fulfill({json:{items:deleted?[]:[readyTemplate]}}))
  page.on('dialog',dialog=>dialog.accept())

  await page.goto('/templates')
  await page.getByRole('button',{name:'Удалить'}).click()

  await expect(page.getByText('Нет шаблонов')).toBeVisible()
  await expect(page.getByText('Добавьте первый PPTX')).toBeVisible()
})

test('selects a template, validates the form and opens a queued job', async ({page}) => {
  await page.route('**/api/v1/templates', route => route.fulfill({json:{items:[readyTemplate]}}))
  await page.route('**/api/v1/jobs', async route => {
    if (route.request().method() === 'POST') return route.fulfill({status:202,json:{job_id:'job-1'}})
    return route.fulfill({json:{items:[]}})
  })
  await page.route('**/api/v1/jobs/job-1', route => route.fulfill({json:{
    id:'job-1',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'processing',
    stage:'planning',progress:45,brief_excerpt:'Итоги квартала',error:null,warnings:[],created_at:new Date().toISOString(),updated_at:new Date().toISOString(),
  }}))
  await page.route('**/api/v1/jobs/job-1/events', route => route.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'}))
  await page.goto('/')
  await page.getByRole('button',{name:'Создать презентацию'}).click()
  await expect(page.locator('textarea[name="brief"]')).toHaveAttribute('required','')
  await page.locator('textarea[name="brief"]').fill('Итоги квартала')
  await page.locator('textarea[name="content_text"]').fill('Выручка выросла на 20%')
  await page.locator('select[name="template_id"]').selectOption('template-1')
  await page.getByRole('button',{name:'Создать презентацию'}).click()
  await expect(page).toHaveURL(/\/jobs\/job-1/)
  await expect(page.getByText('Планирование структуры')).toBeVisible()
  await expect(page.locator('body')).not.toContainText('Â')
})

test('keeps files across picker resets and submits every selected image', async ({page}) => {
  let multipartBody=''
  await page.route('**/api/v1/templates', route => route.fulfill({json:{items:[readyTemplate]}}))
  await page.route('**/api/v1/jobs', async route => {
    if(route.request().method()==='POST'){
      multipartBody=route.request().postDataBuffer()?.toString('utf8')||''
      return route.fulfill({status:202,json:{job_id:'job-files'}})
    }
    return route.fulfill({json:{items:[]}})
  })
  await page.route('**/api/v1/jobs/job-files', route => route.fulfill({json:{
    id:'job-files',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'queued',
    stage:'queued',progress:0,brief_excerpt:'Images',error:null,warnings:[],created_at:new Date().toISOString(),updated_at:new Date().toISOString(),
  }}))
  await page.route('**/api/v1/jobs/job-files/events', route => route.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'}))

  await page.goto('/')
  const picker=page.locator('.content-drop input[type="file"]')
  const alpha={name:'alpha.png',mimeType:'image/png',buffer:Buffer.from('alpha')}
  const beta={name:'beta.png',mimeType:'image/png',buffer:Buffer.from('beta')}
  const gamma={name:'gamma.png',mimeType:'image/png',buffer:Buffer.from('gamma')}

  await picker.setInputFiles(alpha)
  await expect(page.getByText('alpha.png')).toBeVisible()
  await expect(page.getByText('1 из 10 файлов')).toBeVisible()
  await picker.setInputFiles([beta,gamma])
  await expect(page.getByText('3 из 10 файлов')).toBeVisible()
  await page.getByRole('button',{name:'Удалить alpha.png'}).click()
  await picker.setInputFiles(alpha)
  await expect(page.getByText('3 из 10 файлов')).toBeVisible()

  await page.locator('textarea[name="brief"]').fill('Images')
  await page.locator('select[name="template_id"]').selectOption('template-1')
  await page.getByRole('button',{name:'Создать презентацию'}).click()

  await expect.poll(()=>multipartBody).toContain('filename="alpha.png"')
  expect(multipartBody).toContain('filename="beta.png"')
  expect(multipartBody).toContain('filename="gamma.png"')
  expect(multipartBody.match(/name="files"/g)).toHaveLength(3)
})

test('cancels an active job from the progress screen', async ({page}) => {
  const active={
    id:'job-cancel',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'processing',
    stage:'planning',progress:45,brief_excerpt:'Итоги квартала',error:null,warnings:[],validation_issues:[],
    quality_status:'passed',degraded:false,created_at:new Date().toISOString(),updated_at:new Date().toISOString(),
  }
  await page.route('**/api/v1/jobs/job-cancel/cancel', route => route.fulfill({json:{...active,status:'canceled',stage:'canceled'}}))
  await page.route('**/api/v1/jobs/job-cancel', route => route.fulfill({json:active}))
  await page.route('**/api/v1/jobs/job-cancel/events', route => route.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'}))
  page.on('dialog',dialog=>dialog.accept())

  await page.goto('/jobs/job-cancel')
  await page.getByRole('button',{name:'Отменить генерацию'}).click()

  await expect(page.getByText('Отменено')).toBeVisible()
  await expect(page.getByText('Задача отменена')).toBeVisible()
})

test('restores a completed job and exposes preview and download', async ({page}) => {
  await page.route('**/api/v1/jobs/job-2', route => route.fulfill({json:{
    id:'job-2',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'ready',
    stage:'ready',progress:100,brief_excerpt:'Готовая презентация',error:null,warnings:['Проверьте мелкий текст'],
    preview_count:1,created_at:new Date().toISOString(),updated_at:new Date().toISOString(),
  }}))
  await page.route('**/api/v1/jobs/job-2/events', route => route.fulfill({contentType:'text/event-stream',body:'event: job\ndata: {"id":1,"stage":"ready","progress":100,"message":"Готово","created_at":"2026-01-01T00:00:00Z"}\n\n'}))
  await page.route('**/api/v1/jobs/job-2/previews/0', route => route.fulfill({contentType:'image/png',body:''}))
  await page.goto('/jobs/job-2')
  await page.reload()
  await expect(page.getByRole('link',{name:/Скачать PDF/})).toHaveAttribute('href','/api/v1/jobs/job-2/download/pdf')
  await expect(page.getByRole('link',{name:/Скачать PPTX/})).toHaveAttribute('href','/api/v1/jobs/job-2/download')
  await expect(page.getByText('Проверьте мелкий текст')).toBeVisible()
})
