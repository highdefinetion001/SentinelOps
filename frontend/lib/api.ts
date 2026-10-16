export type InventoryServer = {
  server_id: string;
  display_name: string;
  role: string | null;
  lifecycle_status: string;
  reachability: string;
  status: string;
  current_baseline_id: string | null;
  current_baseline_version: number | null;
  current_baseline_fingerprint: string | null;
  last_seen: string | null;
};

export type Baseline = {
  baseline_id: string;
  server_id: string;
  version: number;
  status: string;
  fingerprint: string;
  policy_version: string;
  created_at: string;
  created_by: string;
  snapshot: {
    config: Record<string, unknown>;
    environment: Record<string, unknown>;
    service_group: string;
  };
};

export type ScanServer = {
  server_id: string;
  outcome: "clean" | "drifted" | "pending_baseline" | "unable_to_assess";
  collection_status: "success" | "failed";
  baseline_version: string | null;
  baseline_fingerprint: string | null;
  observed_fingerprint: string | null;
  findings: Array<{
    evidence: {
      field_path: string;
      difference_type: string;
      expected: { state: string; value?: unknown; redacted: boolean };
      actual: { state: string; value?: unknown; redacted: boolean };
    };
    assessment: { severity: string; policy_violation: boolean; policy_ids: string[] };
  }>;
  policy_violations: Array<{
    policy_id: string;
    field_path: string;
    severity: string;
    observed: { state: string; value?: unknown; redacted: boolean };
  }>;
  collection_error: { kind: string; message: string } | null;
  assessment_error: string | null;
};

export type Scan = {
  scan_id: string;
  scanned_at: string;
  status: string;
  detector_version: string;
  total: number;
  successful: number;
  failed: number;
  drifted: number;
  pending_baseline: number;
  unable_to_assess: number;
  servers: ScanServer[];
};

export type ScanSummary = {
  scan_id: string;
  started_at: string;
  completed_at: string;
  status: string;
  total: number;
  successful: number;
  failed: number;
  drifted: number;
  pending_baseline: number;
  unable_to_assess: number;
};

export type Readiness = {
  status: string;
  database: string;
  registered_servers: number;
  demo_controls_enabled: boolean;
};

export type AIAnalysis = {
  analysis_id: string;
  scan_id: string;
  server_ids: string[];
  provider: string;
  model: string | null;
  status: "pending" | "completed" | "failed" | "disabled";
  explanation: {
    format: "structured" | "text";
    summary: string;
    impact: string;
    remediation: string[];
    prevention: string[];
  } | null;
  created_at: string;
  updated_at: string;
  error_summary: string | null;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload && typeof payload.detail === "string" ? payload.detail : response.statusText;
    throw new Error(`${response.status}: ${detail || "Monitor request failed"}`);
  }
  return payload as T;
}

export function describeValue(evidence: { state: string; value?: unknown; redacted: boolean }): string {
  if (evidence.redacted) return "[redacted]";
  if (evidence.state === "missing") return "(missing)";
  if (evidence.state === "null") return "null";
  if (typeof evidence.value === "string") return evidence.value;
  return JSON.stringify(evidence.value);
}
