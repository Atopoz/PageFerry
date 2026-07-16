const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8765';

interface HealthResponse {
  code: 'success';
  data: {
    service: string;
    version: string;
  };
}

export interface ProviderDefinition {
  id: string;
  display_name: string;
  protocol: 'openai' | 'anthropic' | 'gemini' | 'custom';
}

export interface ModelCatalog {
  schema_version: number;
  catalog_version: string;
  providers: ProviderDefinition[];
}

async function requestJson<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { signal });
  if (!response.ok) {
    throw new Error(`Local API returned ${response.status}`);
  }
  return (await response.json()) as T;
}

export function getHealth(signal: AbortSignal): Promise<HealthResponse> {
  return requestJson<HealthResponse>('/healthz', signal);
}

export function getModelCatalog(signal: AbortSignal): Promise<ModelCatalog> {
  return requestJson<ModelCatalog>('/api/v1/model-catalog', signal);
}
