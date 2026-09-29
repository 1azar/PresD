import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import App, { ElapsedTime } from './App'

const user = {id:'user-1',username:'alice'}
const timestamp = '2026-01-01T00:00:00+00:00'
const analysisJob = {id:'analysis-1',status:'ready',stage:'ready',progress:100,error:null,created_at:timestamp,updated_at:'2026-01-01T00:02:00+00:00'}
const readyTemplate = {id:'template-1',name:'Brand.pptx',status:'ready',slide_count:12,error:null,created_at:timestamp,analysis_job:analysisJob}

afterEach(()=>{
  cleanup()
  FakeEventSource.instances=[]
  localStorage.clear()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

class FakeEventSource {
  static instances:FakeEventSource[]=[]
  listeners:Record<string,(event:MessageEvent)=>void>={}
  constructor(){FakeEventSource.instances.push(this)}
  addEventListener=vi.fn((name:string,listener:EventListener)=>{this.listeners[name]=listener as (event:MessageEvent)=>void})
  close=vi.fn()
  onerror:null|(()=>void)=null
  emit(name:string,data:unknown){this.listeners[name]?.({data:JSON.stringify(data)} as MessageEvent)}
}
vi.stubGlobal('EventSource',FakeEventSource)

function mockSession(items:unknown[]=[]){
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request)=>{
    const path=String(input)
    const body=path.endsWith('/auth/me')?user:{items}
    return {ok:true,status:200,json:async()=>body}
  }) as never
}

test('shows login when session is absent', async () => {
  globalThis.fetch = vi.fn().mockResolvedValue({ok:false,json:async()=>({})}) as never
  render(<MemoryRouter><App/></MemoryRouter>)
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Вход'})).toBeInTheDocument())
  expect(screen.queryByText('Создать аккаунт')).not.toBeInTheDocument()
})

test('shows contextual defaults without Dev UI or local persistence', async () => {
  localStorage.setItem('presd.dev.settings',JSON.stringify({useVlm:false,catalogWorkers:8,vlmWorkers:8,fastMode:true}))
  const setItem=vi.spyOn(Storage.prototype,'setItem')
  mockSession()
  render(<MemoryRouter initialEntries={['/templates']}><App/></MemoryRouter>)
  expect(await screen.findByRole('switch',{name:'VLM-анализ'})).toHaveAttribute('aria-checked','true')
  expect(screen.getByRole('combobox',{name:'Потоки каталога'})).toHaveValue('4')
  expect(screen.getByRole('combobox',{name:'Потоки VLM'})).toHaveValue('4')
  expect(screen.getByRole('combobox',{name:'Потоки VLM'})).toBeEnabled()
  expect(screen.queryByRole('button',{name:'Dev'})).not.toBeInTheDocument()
  expect(screen.queryByText('Настройки запуска')).not.toBeInTheDocument()
  expect(setItem).not.toHaveBeenCalled()
})

test('ready templates default to strict generation mode', async () => {
  let submitted:FormData|undefined
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[readyTemplate]})}
    if(path.endsWith('/jobs')&&init?.method==='POST'){submitted=init.body as FormData;return {ok:true,status:202,json:async()=>({job_id:'job-1'})}}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  render(<MemoryRouter><App/></MemoryRouter>)
  const interaction=userEvent.setup()
  await screen.findByRole('option',{name:/Brand\.pptx/})
  expect(screen.queryByLabelText('Настройки анализа')).not.toBeInTheDocument()
  const fast=screen.getByRole('switch',{name:'Быстрый режим'})
  expect(fast).toHaveAttribute('aria-checked','false')
  await interaction.type(screen.getByRole('textbox',{name:'Бриф'}),'Итоги')
  await interaction.type(screen.getByRole('textbox',{name:'Текст'}),'Факты')
  await interaction.selectOptions(screen.getByRole('combobox',{name:'Шаблон'}),'template-1')
  await interaction.click(screen.getByRole('button',{name:/Создать презентацию/}))
  await waitFor(()=>expect(submitted?.get('fast_mode')).toBe('false'))
  expect(submitted?.get('generation_mode')).toBe('strict')
})

test('file picker accumulates selections, deduplicates, removes, and submits all files', async () => {
  let submitted:FormData|undefined
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[readyTemplate]})}
    if(path.endsWith('/jobs')&&init?.method==='POST'){submitted=init.body as FormData;return {ok:true,status:202,json:async()=>({job_id:'job-files'})}}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  render(<MemoryRouter><App/></MemoryRouter>)
  const interaction=userEvent.setup()
  await screen.findByRole('option',{name:/Brand\.pptx/})
  const input=document.querySelector('.content-drop input[type="file"]') as HTMLInputElement
  const json=new File(['{}'],'structured_assets.json',{type:'application/json',lastModified:1})
  const portraits=Array.from({length:5},(_,index)=>new File(['png'],`person-${index+1}.png`,{type:'image/png',lastModified:index+2}))
  await interaction.upload(input,json)
  await interaction.upload(input,portraits)
  await interaction.upload(input,portraits[0])
  expect(within(screen.getByLabelText('Выбранные файлы')).getAllByText(/structured_assets|person-/)).toHaveLength(6)
  await interaction.click(screen.getByRole('button',{name:'Удалить person-5.png'}))
  await interaction.upload(input,portraits[4])
  await interaction.type(screen.getByRole('textbox',{name:'Бриф'}),'NovaCore')
  await interaction.selectOptions(screen.getByRole('combobox',{name:'Шаблон'}),'template-1')
  await interaction.click(screen.getByRole('button',{name:/Создать презентацию/}))
  await waitFor(()=>expect(submitted).toBeDefined())
  expect((submitted?.getAll('files') as File[]).map(file=>file.name)).toEqual([
    'structured_assets.json','person-1.png','person-2.png','person-3.png','person-4.png','person-5.png',
  ])
})

test('degraded result warns before download', async () => {
  const job={id:'job-1',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'ready',stage:'ready',progress:100,brief_excerpt:'Итоги',error:null,warnings:['Использован упрощённый вариант'],validation_issues:[],quality_status:'needs_review',degraded:true,created_at:timestamp,updated_at:timestamp,preview_count:0}
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/jobs/job-1'))return {ok:true,status:200,json:async()=>job}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  render(<MemoryRouter initialEntries={['/jobs/job-1']}><App/></MemoryRouter>)
  const warning=await screen.findByRole('alert')
  const download=screen.getByRole('link',{name:/Скачать PPTX/})
  const pdfDownload=screen.getByRole('link',{name:/Скачать PDF/})
  expect(warning).toHaveTextContent('Результат требует проверки')
  expect(pdfDownload).toHaveAttribute('href','/api/v1/jobs/job-1/download/pdf')
  expect(warning.compareDocumentPosition(pdfDownload)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(warning.compareDocumentPosition(download)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

test('active job can be canceled from its progress screen after confirmation', async () => {
  const active={id:'job-active',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'processing',stage:'planning',progress:45,brief_excerpt:'Итоги',error:null,warnings:[],validation_issues:[],quality_status:'passed',degraded:false,created_at:timestamp,updated_at:timestamp}
  const canceled={...active,status:'canceled',stage:'canceled'}
  let cancelRequests=0
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/jobs/job-active/cancel')&&init?.method==='POST'){cancelRequests+=1;return {ok:true,status:200,json:async()=>canceled}}
    if(path.endsWith('/jobs/job-active'))return {ok:true,status:200,json:async()=>active}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  const confirmation=vi.spyOn(window,'confirm').mockReturnValueOnce(false).mockReturnValue(true)
  render(<MemoryRouter initialEntries={['/jobs/job-active']}><App/></MemoryRouter>)
  const interaction=userEvent.setup()
  const button=await screen.findByRole('button',{name:'Отменить генерацию'})

  await interaction.click(button)
  expect(confirmation).toHaveBeenCalledTimes(1)
  expect(cancelRequests).toBe(0)

  await interaction.click(button)
  await waitFor(()=>expect(screen.getByText('Отменено')).toBeInTheDocument())
  expect(cancelRequests).toBe(1)
  expect(screen.getByText('Задача отменена')).toBeInTheDocument()
  expect(screen.queryByRole('button',{name:'Отменить генерацию'})).not.toBeInTheDocument()
})

test('active job can be canceled directly from history', async () => {
  const active={id:'job-history',type:'generation',template_id:'template-1',template_name:'Brand.pptx',status:'queued',stage:'queued',progress:0,brief_excerpt:'План запуска',error:null,warnings:[],validation_issues:[],quality_status:'passed',degraded:false,created_at:timestamp,updated_at:timestamp}
  const canceled={...active,status:'canceled',stage:'canceled'}
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/jobs/job-history/cancel')&&init?.method==='POST')return {ok:true,status:200,json:async()=>canceled}
    if(path.endsWith('/jobs'))return {ok:true,status:200,json:async()=>({items:[active]})}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  vi.spyOn(window,'confirm').mockReturnValue(true)
  render(<MemoryRouter initialEntries={['/history']}><App/></MemoryRouter>)
  const interaction=userEvent.setup()

  await interaction.click(await screen.findByRole('button',{name:'Отменить'}))

  await waitFor(()=>expect(screen.getByText('Отменено')).toBeInTheDocument())
  expect(screen.queryByRole('button',{name:'Отменить'})).not.toBeInTheDocument()
  expect(screen.getByRole('link',{name:'Открыть: План запуска'})).toBeInTheDocument()
})

test('template deletion requires confirmation and updates the library immediately', async () => {
  let deleteRequests=0
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates/template-1')&&init?.method==='DELETE'){deleteRequests+=1;return {ok:true,status:204,json:async()=>({})}}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[readyTemplate]})}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  const confirmation=vi.spyOn(window,'confirm').mockReturnValueOnce(false).mockReturnValue(true)
  render(<MemoryRouter initialEntries={['/templates']}><App/></MemoryRouter>)
  const interaction=userEvent.setup()
  const button=await screen.findByRole('button',{name:'Удалить'})

  await interaction.click(button)
  expect(confirmation).toHaveBeenCalledWith('Удалить шаблон „Brand.pptx“? Ранее созданные презентации останутся в истории.')
  expect(deleteRequests).toBe(0)
  expect(screen.getByText('Brand.pptx')).toBeInTheDocument()

  await interaction.click(button)
  await waitFor(()=>expect(screen.getByText('Нет шаблонов')).toBeInTheDocument())
  expect(deleteRequests).toBe(1)
  expect(screen.getByText('Добавьте первый PPTX')).toBeInTheDocument()
})

test('template deletion error leaves the card visible and shows the server message', async () => {
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates/template-1')&&init?.method==='DELETE')return {ok:false,status:409,json:async()=>({code:'template_in_use',message:'Дождитесь завершения генерации'})}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[readyTemplate]})}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  vi.spyOn(window,'confirm').mockReturnValue(true)
  render(<MemoryRouter initialEntries={['/templates']}><App/></MemoryRouter>)
  const interaction=userEvent.setup()

  await interaction.click(await screen.findByRole('button',{name:'Удалить'}))

  expect(await screen.findByRole('alert')).toHaveTextContent('Дождитесь завершения генерации')
  expect(screen.getByText('Brand.pptx')).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'Удалить'})).toBeEnabled()
})

test('reanalyze is available for ready and failed templates, uses local settings, and is disabled while processing', async () => {
  const failed={...readyTemplate,id:'template-2',name:'Broken.pptx',status:'failed',slide_count:null,error:'Ошибка',analysis_job:{...analysisJob,id:'analysis-2',status:'failed',stage:'failed',error:'Ошибка'}}
  const processing={...readyTemplate,id:'template-3',name:'Busy.pptx',status:'processing',slide_count:null,analysis_job:{...analysisJob,id:'analysis-3',status:'processing',stage:'building_catalog',progress:50}}
  let payload=''
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request,init?:RequestInit)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[readyTemplate,failed,processing]})}
    if(path.endsWith('/templates/template-1/reanalyze')){payload=String(init?.body);return {ok:true,status:202,json:async()=>({...readyTemplate,status:'processing',analysis_job:{...analysisJob,status:'processing',stage:'queued',progress:0}})}}
    return {ok:true,status:200,json:async()=>processing}
  }) as never
  render(<MemoryRouter initialEntries={['/templates']}><App/></MemoryRouter>)
  const interaction=userEvent.setup()
  const actions=await screen.findAllByRole('button',{name:'Переанализировать'})
  expect(actions).toHaveLength(3)
  expect(actions[0]).toBeEnabled()
  expect(actions[1]).toBeEnabled()
  expect(actions[2]).toBeDisabled()
  const deleteActions=screen.getAllByRole('button',{name:'Удалить'})
  expect(deleteActions[0]).toBeEnabled()
  expect(deleteActions[1]).toBeEnabled()
  expect(deleteActions[2]).toBeDisabled()
  await interaction.click(actions[0])
  const card=within(actions[0].closest('article') as HTMLElement)
  expect(card.getByRole('switch',{name:'VLM-анализ'})).toHaveAttribute('aria-checked','true')
  expect(card.getByRole('combobox',{name:'Потоки VLM'})).toHaveValue('4')
  await interaction.click(card.getByRole('switch',{name:'VLM-анализ'}))
  expect(card.getByRole('combobox',{name:'Потоки VLM'})).toBeDisabled()
  await interaction.selectOptions(card.getByRole('combobox',{name:'Потоки каталога'}),'6')
  await interaction.click(card.getByRole('button',{name:'Запустить анализ'}))
  await waitFor(()=>expect(JSON.parse(payload)).toEqual({use_vlm:false,catalog_workers:6,enrichment_workers:4}))
})

test('failed template analysis replaces progress with an error and retry action', async () => {
  const processing={...readyTemplate,status:'processing',slide_count:null,analysis_job:{...analysisJob,status:'processing',stage:'building_catalog',progress:80}}
  const failed={...processing,status:'failed',error:'Не удалось построить каталог',analysis_job:{...processing.analysis_job,status:'failed',stage:'failed',error:'Не удалось построить каталог'}}
  globalThis.fetch=vi.fn().mockImplementation(async(input:string|URL|Request)=>{
    const path=String(input)
    if(path.endsWith('/auth/me'))return {ok:true,status:200,json:async()=>user}
    if(path.endsWith('/templates/template-1'))return {ok:true,status:200,json:async()=>failed}
    if(path.endsWith('/templates'))return {ok:true,status:200,json:async()=>({items:[processing]})}
    return {ok:true,status:200,json:async()=>({})}
  }) as never
  render(<MemoryRouter initialEntries={['/templates']}><App/></MemoryRouter>)
  expect(await screen.findByRole('progressbar')).toBeInTheDocument()

  act(()=>FakeEventSource.instances[0].emit('job',{id:2,stage:'failed',progress:80,message:'Ошибка',created_at:timestamp}))

  expect(await screen.findByText('Не удалось построить каталог')).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.queryByLabelText(/Прошло/)).not.toBeInTheDocument()
  expect(screen.getByRole('button',{name:'Переанализировать'})).toBeEnabled()
})

test('elapsed time starts at zero, ticks, changes color after five minutes, and freezes at the server end time', () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(timestamp))
  const {rerender}=render(<ElapsedTime startedAt={timestamp}/>)
  expect(screen.getByText('00:00')).not.toHaveClass('overdue')
  act(()=>vi.advanceTimersByTime(300_000))
  expect(screen.getByText('05:00')).not.toHaveClass('overdue')
  act(()=>vi.advanceTimersByTime(1_000))
  expect(screen.getByText('05:01')).toHaveClass('overdue')
  rerender(<ElapsedTime startedAt={timestamp} endedAt="2026-01-01T00:01:15+00:00" active={false}/>)
  expect(screen.getByText('01:15')).not.toHaveClass('overdue')
  act(()=>vi.advanceTimersByTime(60_000))
  expect(screen.getByText('01:15')).toBeInTheDocument()
})
