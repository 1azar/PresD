import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, uploadTemplate } from './api'
import type { AnalysisSettings, GenerationSettings, Job, JobEvent, Template } from './types'
import './degraded.css'

type User = {id:string; username:string}

const DEFAULT_ANALYSIS:AnalysisSettings={useVlm:true,catalogWorkers:4,vlmWorkers:4}
const DEFAULT_GENERATION:GenerationSettings={fastMode:false}
const EMPTY_TEMPLATE:Template={id:'',name:'',status:'processing',slide_count:null,error:null,created_at:'',analysis_job:null}

function Auth({onAuth}:{onAuth:(user:User)=>void}) {
  const [error,setError]=useState(''),navigate=useNavigate()
  const submit=async(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();setError('');const data=new FormData(e.currentTarget);try{const user=await api<User>('/auth/login',{method:'POST',body:JSON.stringify(Object.fromEntries(data))});onAuth(user);navigate('/')}catch(x){setError((x as Error).message)}}
  return <main className="auth"><form className="auth-card" onSubmit={submit}><div className="brand"><span className="brand-mark"/>PresD</div><div className="auth-heading"><h1>Вход</h1><p>Войдите в рабочее пространство.</p></div><label>Логин<input name="username" autoComplete="username" required minLength={3}/></label><label>Пароль<input name="password" type="password" autoComplete="current-password" required minLength={8}/></label>{error&&<div className="error" role="alert">{error}</div>}<button className="primary">Войти</button></form></main>
}

function Toggle({checked,onChange,label,disabled=false}:{checked:boolean;onChange:(value:boolean)=>void;label:string;disabled?:boolean}){
  return <button type="button" className={`toggle ${checked?'on':''}`} role="switch" aria-checked={checked} disabled={disabled} onClick={()=>onChange(!checked)}><span/><b>{label}</b></button>
}

function AnalysisSettingsFields({settings,onChange,disabled=false}:{settings:AnalysisSettings;onChange:(value:AnalysisSettings)=>void;disabled?:boolean}){
  const workerOptions=Array.from({length:8},(_,index)=>index+1)
  return <div className="analysis-settings" aria-label="Настройки анализа"><Toggle checked={settings.useVlm} onChange={value=>onChange({...settings,useVlm:value})} label="VLM-анализ" disabled={disabled}/><label className="number-field"><span>Потоки каталога</span><select value={settings.catalogWorkers} disabled={disabled} onChange={e=>onChange({...settings,catalogWorkers:Number(e.target.value)})}>{workerOptions.map(value=><option value={value} key={value}>{value}</option>)}</select></label><label className="number-field"><span>Потоки VLM</span><select value={settings.vlmWorkers} disabled={disabled||!settings.useVlm} onChange={e=>onChange({...settings,vlmWorkers:Number(e.target.value)})}>{workerOptions.map(value=><option value={value} key={value}>{value}</option>)}</select></label></div>
}

function Shell({user,onLogout}:{user:User;onLogout:()=>void}) {
  const location=useLocation()
  return <div className="shell"><header><Link className="logo" to="/"><span className="brand-mark"/>PresD</Link><nav aria-label="Основная навигация"><Link className={location.pathname==='/'?'active':''} to="/">Создать</Link><Link className={location.pathname==='/templates'?'active':''} to="/templates">Шаблоны</Link><Link className={location.pathname==='/history'?'active':''} to="/history">История</Link></nav><div className="header-actions"><div className="account"><span>{user.username.slice(0,1).toUpperCase()}</span><b>{user.username}</b><button onClick={onLogout} aria-label="Выйти">↗</button></div></div></header><div className="workspace"><Routes><Route path="/" element={<Generator/>}/><Route path="/templates" element={<Templates/>}/><Route path="/history" element={<History/>}/><Route path="/jobs/:id" element={<JobView/>}/><Route path="*" element={<Navigate to="/"/>}/></Routes></div></div>
}

const templateStage:Record<string,string>={queued:'В очереди',validating:'Проверка файла',analyzing_structure:'Анализ структуры',building_base_catalog:'Построение базового каталога',ready_enriching:'Готов к работе — улучшаем визуальный анализ',enriching_visuals:'Улучшаем визуальный анализ',rebuilding_catalog:'Перестраиваем визуальный каталог',enrichment_failed:'Базовая версия готова; визуальный анализ не завершён',ready:'Шаблон готов',failed:'Ошибка анализа'}

function ProgressBar({value,active=true}:{value:number;active?:boolean}){
  return <div className={`progress ${active?'active':''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value}><span style={{width:`${Math.min(100,Math.max(0,value))}%`}}/></div>
}

function timestamp(value:string|number){
  if(typeof value==='number')return value
  return Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(value)?value:`${value}Z`)
}

export function ElapsedTime({startedAt,endedAt,active=true}:{startedAt:string|number;endedAt?:string|number;active?:boolean}){
  const [now,setNow]=useState(()=>Date.now())
  useEffect(()=>{
    if(!active)return
    setNow(Date.now())
    const timer=window.setInterval(()=>setNow(Date.now()),1000)
    return()=>clearInterval(timer)
  },[active,startedAt])
  const start=timestamp(startedAt),end=endedAt===undefined?now:timestamp(endedAt)
  const seconds=Number.isFinite(start)&&Number.isFinite(end)?Math.max(0,Math.floor((end-start)/1000)):0
  const value=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`
  return <span className={`elapsed-time ${seconds>300?'overdue':''}`} aria-label={`Прошло ${value}`}>{value}</span>
}

function ProgressValue({value,startedAt,endedAt,active=true}:{value:number;startedAt:string|number;endedAt?:string|number;active?:boolean}){
  return <b className="progress-value"><span>{value}%</span><ElapsedTime startedAt={startedAt} endedAt={endedAt} active={active}/></b>
}

function AnalysisProgress({template,compact=false}:{template:Template;compact?:boolean}){
  const job=template.analysis_job
  if(!job)return null
  const failed=job.status==='failed'||job.stage==='failed'
  if(failed)return <div className={`analysis-progress ${compact?'compact':''}`} aria-live="polite"><p className="inline-error" role="alert">{job.error||template.error||'Анализ завершился с ошибкой'}</p></div>
  const active=!failed&&job.status!=='ready'
  return <div className={`analysis-progress ${compact?'compact':''}`} aria-live="polite"><div className="progress-line"><span>{templateStage[job.stage]||job.stage}</span><ProgressValue value={job.progress} startedAt={job.created_at} endedAt={active?undefined:job.updated_at} active={active}/></div><ProgressBar value={job.progress} active={active}/>{job.warnings?.map((warning,index)=><p className="analysis-warning" key={index}>{warning}</p>)}</div>
}

function useTrackedTemplate(initial:Template,onChange?:(value:Template)=>void){
  const [value,setValue]=useState(initial)
  useEffect(()=>setValue(initial),[initial])
  useEffect(()=>{
    const job=value.analysis_job
    if(!job||!['queued','processing'].includes(job.status))return
    let timer:number|undefined
    const refresh=()=>api<Template>(`/templates/${value.id}`).then(item=>{setValue(item);onChange?.(item)}).catch(()=>{})
    const stream=new EventSource(`/api/v1/jobs/${job.id}/events`)
    stream.addEventListener('job',event=>{
      const update=JSON.parse((event as MessageEvent).data) as JobEvent
      setValue(old=>({...old,status:update.stage==='ready'?'ready':update.stage==='failed'?'failed':old.status,analysis_job:old.analysis_job?{...old.analysis_job,stage:update.stage,progress:update.progress,status:['ready','enrichment_failed'].includes(update.stage)?'ready':update.stage==='failed'?'failed':'processing'}:null}))
      if(['ready_enriching','ready','failed','enrichment_failed'].includes(update.stage))refresh()
    })
    stream.onerror=()=>{stream.close();timer=window.setInterval(refresh,3000)}
    return()=>{stream.close();if(timer)clearInterval(timer)}
  },[value.id,value.analysis_job?.id,value.analysis_job?.status])
  return value
}

function TemplateUpload({onDone}:{onDone:(template:Template)=>void}) {
  const [settings,setSettings]=useState<AnalysisSettings>({...DEFAULT_ANALYSIS}),[uploadProgress,setUploadProgress]=useState<number|null>(null),[uploadStartedAt,setUploadStartedAt]=useState<number>(),[current,setCurrent]=useState<Template|null>(null),[error,setError]=useState('')
  const tracked=useTrackedTemplate(current||EMPTY_TEMPLATE,item=>{setCurrent(item);onDone(item)})
  const upload=async(file?:File)=>{if(!file)return;if(file.size>100*1024*1024){setError('Файл превышает 100 МБ');return}const operationSettings=settings;setSettings({...DEFAULT_ANALYSIS});setError('');setCurrent(null);setUploadProgress(0);try{const item=await uploadTemplate(file,operationSettings,setUploadProgress,()=>setUploadStartedAt(Date.now()));setCurrent(item);onDone(item)}catch(x){setError((x as Error).message)}finally{setUploadProgress(null);setUploadStartedAt(undefined)}}
  const reset=()=>{setCurrent(null);setSettings({...DEFAULT_ANALYSIS})}
  if(current)return <div className="upload-status"><div className="file-row"><span className="file-icon">P</span><div><b>{current.name}</b><small>{tracked.status==='ready'?`${tracked.slide_count||0} слайдов`:tracked.status==='failed'?'Анализ не завершён':'PPTX загружен'}</small></div>{tracked.status==='ready'&&<span className="success-mark">✓</span>}</div>{tracked.analysis_job&&<AnalysisProgress template={tracked}/>} {tracked.status==='ready'&&<button type="button" className="secondary full" onClick={reset}>Загрузить ещё</button>}{tracked.status==='failed'&&<button type="button" className="secondary full" onClick={reset}>Загрузить другой</button>}</div>
  if(uploadProgress!==null)return <div className="upload-status" aria-live="polite"><div className="progress-line"><span>Загрузка файла</span><ProgressValue value={uploadProgress} startedAt={uploadStartedAt??Date.now()}/></div><ProgressBar value={uploadProgress}/></div>
  return <><AnalysisSettingsFields settings={settings} onChange={setSettings}/><label className="drop" onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();upload(e.dataTransfer.files[0])}}><input type="file" accept=".pptx" onChange={e=>upload(e.target.files?.[0])}/><span className="upload-icon">↑</span><b>Загрузить PPTX</b><small>Перетащите файл или нажмите для выбора · до 100 МБ</small></label>{error&&<div className="error" role="alert">{error}</div>}</>
}

function useTemplates(){
  const [items,setItems]=useState<Template[]>([])
  const removed=useRef(new Set<string>())
  const load=()=>api<{items:Template[]}>('/templates').then(value=>setItems(value.items.filter(item=>!removed.current.has(item.id)))).catch(()=>{})
  const update=(item:Template)=>{if(removed.current.has(item.id))return;setItems(old=>old.some(x=>x.id===item.id)?old.map(x=>x.id===item.id?item:x):[item,...old])}
  const restore=(item:Template)=>{removed.current.delete(item.id);setItems(old=>old.some(x=>x.id===item.id)?old.map(x=>x.id===item.id?item:x):[item,...old])}
  const remove=(id:string)=>{removed.current.add(id);setItems(old=>old.filter(item=>item.id!==id))}
  useEffect(()=>{load();const timer=window.setInterval(load,5000);return()=>clearInterval(timer)},[])
  return {items,load,update,restore,remove}
}

function Generator(){
  const {items:templates,update}=useTemplates(),[generationSettings,setGenerationSettings]=useState<GenerationSettings>({...DEFAULT_GENERATION}),[files,setFiles]=useState<File[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),navigate=useNavigate()
  const ready=templates.filter(x=>x.status==='ready')
  const processing=templates.filter(x=>x.status==='processing')
  const addFiles=(selected:File[])=>{setFiles(current=>{const next=[...current];for(const file of selected){const duplicate=next.some(item=>item.name===file.name&&item.size===file.size&&item.lastModified===file.lastModified);if(!duplicate&&next.length<10)next.push(file)}return next})}
  const removeFile=(index:number)=>setFiles(current=>current.filter((_,itemIndex)=>itemIndex!==index))
  const submit=async(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();setBusy(true);setError('');const form=new FormData(e.currentTarget);files.forEach(file=>form.append('files',file));form.append('fast_mode',String(generationSettings.fastMode));form.append('generation_mode',generationSettings.fastMode?'reliable':'strict');setGenerationSettings({...DEFAULT_GENERATION});try{const {job_id}=await api<{job_id:string}>('/jobs',{method:'POST',body:form});navigate(`/jobs/${job_id}`)}catch(x){setError((x as Error).message);setBusy(false)}}
  return <main className="page narrow"><div className="page-title"><h1>Новая презентация</h1><p>Добавьте задачу, материалы и выберите шаблон.</p></div><form className="generator" onSubmit={submit}><section className="form-section"><div className="section-heading"><span>1</span><div><h2>Задача</h2><p>Коротко опишите ожидаемый результат.</p></div></div><label className="field">Бриф<textarea name="brief" maxLength={5000} required placeholder="Например: итоги квартала для совета директоров"/></label></section><section className="form-section"><div className="section-heading"><span>2</span><div><h2>Материалы</h2><p>Текст, документы, таблицы и изображения для презентации.</p></div></div><label className="field">Текст<textarea name="content_text" placeholder="Вставьте факты, заметки или структуру"/></label><label className="drop content-drop"><input type="file" multiple accept=".txt,.md,.pdf,.docx,.csv,.xlsx,.json,.png,.jpg,.jpeg" onChange={e=>{const selected=Array.from(e.currentTarget.files??[]);addFiles(selected);e.currentTarget.value=''}}/><span className="upload-icon">＋</span><b>{files.length?`${files.length} из 10 файлов`:'Добавить файлы'}</b><small>{files.length?'Можно выбрать остальные файлы ещё одним действием':'TXT, MD, PDF, DOCX, CSV, XLSX, JSON, PNG, JPEG · до 10 файлов'}</small></label>{files.length>0&&<div className="selected-files" aria-label="Выбранные файлы">{files.map((file,index)=><div className="selected-file" key={`${file.name}:${file.size}:${file.lastModified}`}><span title={file.name}>{file.name}</span><button type="button" aria-label={`Удалить ${file.name}`} onClick={()=>removeFile(index)}>×</button></div>)}</div>}</section><section className="form-section"><div className="section-heading"><span>3</span><div><h2>Параметры</h2><p>Диапазон слайдов и шаблон.</p></div></div><div className="form-grid"><label className="field">Количество слайдов<div className="range"><input name="slide_min" type="number" min="1" max="30" defaultValue="10"/><span>—</span><input name="slide_max" type="number" min="1" max="30" defaultValue="15"/></div></label><div className="field"><label htmlFor="template">Шаблон</label>{ready.length?<select id="template" name="template_id" required defaultValue=""><option value="" disabled>Выберите шаблон</option>{ready.map(x=><option value={x.id} key={x.id}>{x.name} · {x.slide_count} сл.</option>)}</select>:<TemplateUpload onDone={update}/>}</div></div>{processing.map(item=><TrackedAnalysis key={item.id} item={item} onChange={update}/>)}</section>{error&&<div className="error" role="alert">{error}</div>}<div className="launch-area"><div className="fast-mode"><Toggle checked={generationSettings.fastMode} onChange={fastMode=>setGenerationSettings({fastMode})} label="Быстрый режим"/><small>Отключает ансамбль генерации и смысловую проверку.</small></div><button className="primary launch" disabled={busy||!ready.length}>{busy?'Запуск…':'Создать презентацию'}<span>→</span></button></div></form></main>
}

function TrackedAnalysis({item,onChange}:{item:Template;onChange:(item:Template)=>void}){
  const tracked=useTrackedTemplate(item,onChange)
  return <div className="inline-analysis"><b>{tracked.name}</b><AnalysisProgress template={tracked} compact/></div>
}

function TemplateCard({item,onChange,onReanalyze,onDelete}:{item:Template;onChange:(item:Template)=>void;onReanalyze:(item:Template,settings:AnalysisSettings)=>Promise<void>;onDelete:(item:Template)=>Promise<void>}){
  const tracked=useTrackedTemplate(item,onChange),[expanded,setExpanded]=useState(false),[settings,setSettings]=useState<AnalysisSettings>({...DEFAULT_ANALYSIS}),[busy,setBusy]=useState<'reanalyze'|'delete'|null>(null)
  const processing=tracked.status==='processing'||tracked.analysis_job?.status==='queued'||tracked.analysis_job?.status==='processing'
  const open=()=>{setSettings({...DEFAULT_ANALYSIS});setExpanded(true)}
  const run=async()=>{setBusy('reanalyze');try{await onReanalyze(tracked,settings);setExpanded(false);setSettings({...DEFAULT_ANALYSIS})}catch{/* error is shown by the templates screen */}finally{setBusy(null)}}
  const remove=async()=>{if(!window.confirm(`Удалить шаблон „${tracked.name}“? Ранее созданные презентации останутся в истории.`))return;setBusy('delete');try{await onDelete(tracked)}catch{/* error is shown by the templates screen */}finally{setBusy(null)}}
  return <article className="template-card"><div className="thumb">{tracked.status==='ready'?<img src={`/api/v1/templates/${tracked.id}/preview`} alt=""/>:<div className="thumb-placeholder"><span className={processing?'spinner':''}/><small>{tracked.status==='failed'?'Ошибка':'Обработка'}</small></div>}</div><div className="card-body"><div className="card-top"><span className={`badge ${tracked.status}`}>{tracked.status==='ready'?'Готов':processing?'Анализ':'Ошибка'}</span><div className="card-actions"><button className="link-button" type="button" disabled={processing||Boolean(busy)} onClick={open}>Переанализировать</button><button className="link-button danger-action" type="button" disabled={processing||Boolean(busy)} onClick={remove}>{busy==='delete'?'Удаление…':'Удалить'}</button></div></div><h2 title={tracked.name}>{tracked.name}</h2>{(processing||Boolean(tracked.analysis_job?.warnings?.length))&&<AnalysisProgress template={tracked} compact/>}{tracked.status==='ready'&&<p>{tracked.slide_count} слайдов</p>}{tracked.status==='failed'&&<p className="inline-error" role="alert">{tracked.error||tracked.analysis_job?.error||'Анализ завершился с ошибкой'}</p>}{expanded&&!processing&&<div className="reanalyze-settings"><AnalysisSettingsFields settings={settings} onChange={setSettings} disabled={Boolean(busy)}/><div className="reanalyze-actions"><button className="primary" type="button" disabled={Boolean(busy)} onClick={run}>{busy==='reanalyze'?'Запуск…':'Запустить анализ'}</button><button className="secondary" type="button" disabled={Boolean(busy)} onClick={()=>setExpanded(false)}>Отмена</button></div></div>}</div></article>
}

function Templates(){
  const {items,update,restore,remove}=useTemplates(),[error,setError]=useState('')
  const reanalyze=async(item:Template,settings:AnalysisSettings)=>{setError('');try{const value=await api<Template>(`/templates/${item.id}/reanalyze`,{method:'POST',body:JSON.stringify({use_vlm:settings.useVlm,catalog_workers:settings.catalogWorkers,enrichment_workers:settings.vlmWorkers})});update(value)}catch(x){setError((x as Error).message);throw x}}
  const deleteItem=async(item:Template)=>{setError('');try{await api<void>(`/templates/${item.id}`,{method:'DELETE'});remove(item.id)}catch(x){setError((x as Error).message);throw x}}
  return <main className="page"><div className="bar"><div><h1>Шаблоны</h1><p>{items.length?`${items.length} в библиотеке`:'Добавьте первый PPTX'}</p></div><div className="upload-box"><TemplateUpload onDone={restore}/></div></div>{error&&<div className="error" role="alert">{error}</div>}{items.length?<div className="cards">{items.map(item=><TemplateCard item={item} onChange={update} onReanalyze={reanalyze} onDelete={deleteItem} key={item.id}/>)}</div>:<div className="empty"><span className="empty-icon">▧</span><h2>Нет шаблонов</h2><p>Загрузите PPTX, чтобы использовать его при генерации.</p></div>}</main>
}

const status=(value:string)=>({queued:'В очереди',processing:'В работе',ready:'Готово',failed:'Ошибка',canceled:'Отменено'}[value]||value)
const jobStage:Record<string,string>={queued:'Задача в очереди',extracting_content:'Извлечение материалов',planning:'Планирование структуры',materializing_assets:'Восстановление недостающих данных',content_analysis:'Анализ содержания',planning_outline:'Планирование сценария',generating_slides:'Генерация слайдов',evaluating_quality:'Проверка качества',drafting_candidates:'Создание вариантов',evaluating_candidates:'Сравнение и смысловая проверка',revising:'Исправление слайдов',building_and_qa:'Сборка и проверка',ready:'Презентация готова',failed:'Произошла ошибка',canceled:'Задача отменена'}

const isActiveJob=(job:Job)=>job.status==='queued'||job.status==='processing'
const confirmCancellation=()=>window.confirm('Отменить генерацию? Текущий прогресс будет потерян.')

function History(){
  const [jobs,setJobs]=useState<Job[]>([]),[canceling,setCanceling]=useState<string>(),[error,setError]=useState('')
  useEffect(()=>{api<{items:Job[]}>('/jobs').then(x=>setJobs(x.items))},[])
  const cancel=async(job:Job)=>{if(!confirmCancellation())return;setCanceling(job.id);setError('');try{const value=await api<Job>(`/jobs/${job.id}/cancel`,{method:'POST'});setJobs(old=>old.map(item=>item.id===value.id?value:item))}catch(x){setError((x as Error).message)}finally{setCanceling(undefined)}}
  return <main className="page"><div className="page-title"><h1>История</h1><p>Последние задания и готовые файлы.</p></div>{error&&<div className="error history-error" role="alert">{error}</div>}{jobs.length?<div className="history"><div className="history-head"><span>Дата</span><span>Презентация</span><span>Шаблон</span><span>Статус</span><span/></div>{jobs.map(j=><div className="history-row" key={j.id}><time>{new Date(j.created_at).toLocaleDateString('ru-RU')}</time><Link to={`/jobs/${j.id}`} className="history-title">{j.brief_excerpt}</Link><span>{j.template_name}</span><span className={`badge ${j.status}`}>{status(j.status)}</span>{isActiveJob(j)?<button className="cancel-link" type="button" disabled={canceling===j.id} onClick={()=>cancel(j)}>{canceling===j.id?'…':'Отменить'}</button>:<Link to={`/jobs/${j.id}`} className="history-arrow" aria-label={`Открыть: ${j.brief_excerpt}`}>→</Link>}</div>)}</div>:<div className="empty"><span className="empty-icon">≡</span><h2>История пуста</h2><p>Здесь появятся запущенные задания.</p></div>}</main>
}

function JobView(){
  const {id}=useParams(),[job,setJob]=useState<Job>(),[events,setEvents]=useState<JobEvent[]>([]),[canceling,setCanceling]=useState(false),[cancelError,setCancelError]=useState('')
  useEffect(()=>{if(!id)return;let timer:number|undefined;const load=()=>api<Job>(`/jobs/${id}`).then(setJob);load();const stream=new EventSource(`/api/v1/jobs/${id}/events`);stream.addEventListener('job',e=>{const item=JSON.parse((e as MessageEvent).data);setEvents(old=>old.some(x=>x.id===item.id)?old:[...old,item]);load()});stream.onerror=()=>{stream.close();timer=window.setInterval(load,3000)};return()=>{stream.close();if(timer)clearInterval(timer)}},[id])
  if(!job)return <main className="page"><div className="loading"><span className="spinner"/>Загрузка задания</div></main>
  const active=isActiveJob(job)
  const cancel=async()=>{if(!confirmCancellation())return;setCanceling(true);setCancelError('');try{setJob(await api<Job>(`/jobs/${job.id}/cancel`,{method:'POST'}))}catch(x){setCancelError((x as Error).message)}finally{setCanceling(false)}}
  const overline=job.status==='ready'?'РЕЗУЛЬТАТ':active?'ТЕКУЩИЙ ЭТАП':'СТАТУС'
  return <main className="page narrow"><Link to="/history" className="back">← История</Link><div className="result-head"><span className={`badge ${job.status}`}>{status(job.status)}</span><h1>{job.brief_excerpt}</h1><p>{job.template_name}</p></div><section className="progress-card"><div className="progress-title"><div><span className="overline">{overline}</span><h2>{jobStage[job.stage]||job.stage}</h2></div><strong><span>{job.progress}%</span><ElapsedTime startedAt={job.created_at} endedAt={active?undefined:job.updated_at} active={active}/></strong></div><ProgressBar value={job.progress} active={active}/>{active&&<div className="cancel-actions"><button className="danger-button" type="button" disabled={canceling} onClick={cancel}>{canceling?'Отменяем…':'Отменить генерацию'}</button></div>}{cancelError&&<div className="error" role="alert">{cancelError}</div>}{job.error&&<div className="error" role="alert">{job.error}</div>}{job.status==='failed'&&job.validation_issues?.length>0&&<div className="warnings"><b>Детали ошибки</b>{job.validation_issues.map((x,i)=><p key={i}><code>{x.code}</code>{x.path&&<> · {x.path}</>} — {x.message}</p>)}</div>}<div className="events">{events.map(e=><div key={e.id}><time>{new Date(e.created_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}</time><span>{e.message}</span></div>)}</div></section>{job.status==='ready'&&<>{job.degraded&&<div className="degraded-warning" role="alert"><b>Результат требует проверки</b><p>Презентация создана, но не достигла всех смысловых порогов качества. Просмотрите её перед использованием.</p>{job.warnings.map((x,i)=><p key={i}>{x}</p>)}</div>}<div className="done-actions"><a className="secondary" href={`/api/v1/jobs/${job.id}/download/pdf`}>Скачать PDF <span>↓</span></a><a className="primary" href={`/api/v1/jobs/${job.id}/download`}>Скачать PPTX <span>↓</span></a></div>{!job.degraded&&job.warnings.length>0&&<div className="warnings"><b>Замечания проверки</b>{job.warnings.map((x,i)=><p key={i}>{x}</p>)}</div>}<div className="previews">{Array.from({length:job.preview_count||1},(_,i)=><img key={i} src={`/api/v1/jobs/${job.id}/previews/${i}`} alt={`Слайд ${i+1}`}/>)}</div></>}</main>
}

export default function App(){
  const [user,setUser]=useState<User|null|undefined>(undefined)
  useEffect(()=>{api<User>('/auth/me').then(setUser).catch(()=>setUser(null))},[])
  if(user===undefined)return <div className="splash"><span className="brand-mark"/>PresD</div>
  if(!user)return <Routes><Route path="*" element={<Auth onAuth={setUser}/>}/></Routes>
  return <Shell user={user} onLogout={async()=>{await api('/auth/logout',{method:'POST'});setUser(null)}}/>
}
