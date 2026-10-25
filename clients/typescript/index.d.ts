export type Provenance = "own-index" | "open-data" | "aggregator" | "undisclosed" | "user-operated";
export type OutcomeStatus = "success" | "partial" | "empty" | "rate_limited" | "failed";

export interface SearchParams {
  engine?: string;
  mode?: "fast" | "research" | "private";
  vertical?: "web" | "local";
  num?: number;
  offset?: number;
  country?: string;
  language?: string;
  safe?: boolean;
  freshness?: "h" | "d" | "w" | "m" | "y";
  location?: string;
  strict_params?: boolean;
}

export interface SourceRef { engine: string; rank: number; provenance: Provenance }

export interface RankedResult {
  rank: number;
  url: string;
  canonical_url: string;
  title: string;
  snippet: string;
  published_at: string | null;
  updated_at: string | null;
  source_name: string | null;
  sources: SourceRef[];
}

export interface ProviderReport {
  engine: string;
  role: string;
  status: "ok" | "empty" | "failed" | "rate_limited" | "timeout" | "skipped";
  cached: boolean;
  latency_ms: number | null;
  results: number;
  error: string | null;
  retry_after: number | null;
  applied_params: string[];
  ignored_params: string[];
}

export interface SearchOutcome {
  id: string;
  status: OutcomeStatus;
  created_at: string;
  elapsed_ms: number;
  requested_engine: string;
  resolved_engine: string;
  params: { applied: string[]; ignored: string[] };
  providers: ProviderReport[];
  results: RankedResult[];
  places: Array<Record<string, unknown>>;
  entity: Record<string, unknown> | null;
  answer: { text: string; type: string; generated: boolean; citations: string[] } | null;
  related: string[];
  has_more: boolean;
  attribution: string[];
  rights: Record<string, Record<string, unknown>>;
  retention: { expires_at: string | null; reason: string | null };
  error: string | null;
}

export class FairserpError extends Error {
  status: number;
  body: unknown;
}

export class Fairserp {
  constructor(options: { baseUrl: string; apiKey: string; fetch?: typeof fetch; timeoutMs?: number });
  search(q: string, params?: SearchParams): Promise<SearchOutcome>;
  serpapi(params: Record<string, string>): Promise<Record<string, unknown>>;
  engines(): Promise<Record<string, unknown>>;
}
