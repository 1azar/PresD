import { expect, test, vi } from 'vitest'
import { uploadTemplate } from './api'

test('upload sends analysis settings and starts timing immediately before XHR send', async()=>{
  let sent:FormData|undefined
  let started=false
  class FakeXMLHttpRequest {
    upload:{onprogress?:()=>void}={}
    status=202
    responseText=JSON.stringify({id:'template-1'})
    onerror?:()=>void
    onload?:()=>void
    open=vi.fn()
    send(body:FormData){
      expect(started).toBe(true)
      sent=body
      this.onload?.()
    }
    withCredentials=false
  }
  vi.stubGlobal('XMLHttpRequest',FakeXMLHttpRequest)
  await uploadTemplate(new File(['pptx'],'brand.pptx'),{useVlm:false,catalogWorkers:7,vlmWorkers:3},vi.fn(),()=>{started=true})
  expect(sent?.get('use_vlm')).toBe('false')
  expect(sent?.get('catalog_workers')).toBe('7')
  expect(sent?.get('enrichment_workers')).toBe('3')
})
