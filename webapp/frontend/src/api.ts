import type { AnalysisSettings, Template } from './types'

const message = async (response: Response) => {
  const body = await response.json().catch(() => ({}))
  throw new Error(body.message || 'Не удалось выполнить запрос')
}

export async function api<T>(path:string, init?:RequestInit):Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials:'include', ...init, headers: init?.body instanceof FormData ? init.headers : {'Content-Type':'application/json', ...init?.headers} })
  if (!response.ok) return message(response)
  if (response.status === 204) return undefined as T
  return response.json()
}

export function uploadTemplate(file:File, settings:AnalysisSettings, onProgress:(value:number)=>void, onStart?:()=>void):Promise<Template> {
  return new Promise((resolve,reject)=>{
    const request=new XMLHttpRequest()
    request.open('POST','/api/v1/templates')
    request.withCredentials=true
    request.upload.onprogress=event=>{
      if(event.lengthComputable)onProgress(Math.round(event.loaded/event.total*100))
    }
    request.onerror=()=>reject(new Error('Не удалось загрузить шаблон'))
    request.onload=()=>{
      let body:Record<string,unknown>={}
      try{body=JSON.parse(request.responseText||'{}')}catch{/* response is not JSON */}
      if(request.status<200||request.status>=300){reject(new Error(String(body.message||'Не удалось загрузить шаблон')));return}
      onProgress(100)
      resolve(body as Template)
    }
    const body=new FormData()
    body.append('file',file)
    body.append('use_vlm',String(settings.useVlm))
    body.append('catalog_workers',String(settings.catalogWorkers))
    body.append('enrichment_workers',String(settings.vlmWorkers))
    onStart?.()
    request.send(body)
  })
}
