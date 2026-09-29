export type AnalysisJob = { id:string; status:'queued'|'processing'|'ready'|'failed'; stage:string; progress:number; error:string|null; warnings:string[]; created_at:string; updated_at:string }
export type Template = { id:string; name:string; status:'processing'|'ready'|'failed'; slide_count:number|null; error:string|null; created_at:string; analysis_job:AnalysisJob|null }
export type ValidationIssue = {code:string; path:string; message:string}
export type Job = { id:string; type:string; template_id:string; template_name:string; status:'queued'|'processing'|'ready'|'failed'|'canceled'; stage:string; progress:number; brief_excerpt:string; error:string|null; warnings:string[]; validation_issues:ValidationIssue[]; quality_status:'passed'|'needs_review'; result_kind?:'primary'|'fallback'; degraded:boolean; created_at:string; updated_at:string; preview_count?:number }
export type JobEvent = { id:number; stage:string; progress:number; message:string; created_at:string }
export type AnalysisSettings = { useVlm:boolean; catalogWorkers:number; vlmWorkers:number }
export type GenerationSettings = { fastMode:boolean }
