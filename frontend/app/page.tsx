"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownToLine,
  Check,
  ChevronDown,
  CircleHelp,
  Clock3,
  Database,
  Fingerprint,
  Play,
  RefreshCw,
  Sparkles,
  RotateCcw,
  Shield,
  ShieldAlert,
  Server,
  Wifi,
  WifiOff,
} from "lucide-react";
import {
  api,
  describeValue,
  type AIAnalysis,
  type Baseline,
  type InventoryServer,
  type ProvisionServerRequest,
  type ProvisioningResult,
  type Readiness,
  type Scan,
  type ScanSummary,
  type ServerTemplate,
} from "@/lib/api";

type Health = { status: string; application: string; dependencies_checked: boolean };
type FleetState = {
  server_id: string;
  status: "success" | "failed";
  fingerprint: string | null;
  role: string | null;
  error: { kind: string; message: string } | null;
};
type CandidateGroups = Record<string, Baseline[]>;
type Operation = { serverId: string; kind: "approve" | "drift" | "reset" } | null;

const formatDate = (value?: string | null) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
};

const shortHash = (value?: string | null) => (value ? `${value.slice(0, 12)}…${value.slice(-6)}` : "Not collected");

function createRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function StatusPill({ value }: { value: string }) {
  return <span className={`status-pill status-${value.toLowerCase().replaceAll("_", "-")}`}>{value.replaceAll("_", " ")}</span>;
}

function Metric({ label, value, tone, icon: Icon }: { label: string; value: number | string; tone: string; icon: typeof Server }) {
  return (
    <article className="metric-card">
      <span className={`metric-icon ${tone}`}><Icon size={17} /></span>
      <div><span className="metric-label">{label}</span><strong>{value}</strong></div>
    </article>
  );
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [servers, setServers] = useState<InventoryServer[]>([]);
  const [fleetStates, setFleetStates] = useState<FleetState[]>([]);
  const [candidates, setCandidates] = useState<CandidateGroups>({});
  const [history, setHistory] = useState<ScanSummary[]>([]);
  const [activeScan, setActiveScan] = useState<Scan | null>(null);
  const [selectedScanId, setSelectedScanId] = useState("");
  const [actor, setActor] = useState("demo-operator");
  const [scenario, setScenario] = useState("security_drift");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<Operation>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dataLoadedAt, setDataLoadedAt] = useState("");
  const [aiAnalysis, setAiAnalysis] = useState<AIAnalysis | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState("");
  const [aiRefreshKey, setAiRefreshKey] = useState(0);
  const [provisionFormOpen, setProvisionFormOpen] = useState(false);
  const [templates, setTemplates] = useState<ServerTemplate[]>([]);
  const [provisionRole, setProvisionRole] = useState<ServerTemplate["role"]>("web");
  const [provisionTemplateId, setProvisionTemplateId] = useState<ServerTemplate["template_id"]>("web-default-v1");
  const [provisionServerId, setProvisionServerId] = useState("");
  const [provisionDisplayName, setProvisionDisplayName] = useState("");
  const [provisionDescription, setProvisionDescription] = useState("");
  const [provisioning, setProvisioning] = useState(false);
  const [provisionRequestId, setProvisionRequestId] = useState("");

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError("");
    const [healthResult, readyResult, collectionResult, historyResult] = await Promise.allSettled([
      api<Health>("/api/health"),
      api<Readiness>("/api/ready"),
      api<{ servers: FleetState[] }>("/api/fleet/states"),
      api<{ items: ScanSummary[] }>("/api/scans?limit=20&offset=0"),
    ]);
    // Fleet collection reconciles discoveries and may create pending candidates.
    // Read inventory afterward so the displayed enrollment state reflects that result.
    const [inventoryResult] = await Promise.allSettled([
      api<{ items: InventoryServer[] }>("/api/servers?limit=100&offset=0"),
    ]);
    if (healthResult.status === "fulfilled") setHealth(healthResult.value);
    else setHealth(null);
    if (readyResult.status === "fulfilled") setReadiness(readyResult.value);
    else setReadiness(null);
    if (inventoryResult.status === "fulfilled") setServers(inventoryResult.value.items);
    if (collectionResult.status === "fulfilled") setFleetStates(collectionResult.value.servers);
    if (historyResult.status === "fulfilled") setHistory(historyResult.value.items);

    const failures = [inventoryResult, collectionResult, historyResult].filter((result) => result.status === "rejected");
    if (failures.length) setError((failures[0] as PromiseRejectedResult).reason instanceof Error
      ? (failures[0] as PromiseRejectedResult).reason.message
      : "Monitor data could not be loaded");

    if (inventoryResult.status === "fulfilled") {
      const candidateResults = await Promise.allSettled(inventoryResult.value.items.map(async (server) => {
        const response = await api<{ items: Baseline[] }>(`/api/servers/${encodeURIComponent(server.server_id)}/baseline-candidates`);
        return [server.server_id, response.items] as const;
      }));
      const nextCandidates: CandidateGroups = {};
      for (const result of candidateResults) {
        if (result.status === "fulfilled") nextCandidates[result.value[0]] = result.value[1];
      }
      setCandidates(nextCandidates);
    }

    if (historyResult.status === "fulfilled") {
      const latest = historyResult.value.items[0];
      if (latest) {
        setSelectedScanId((current) => current || latest.scan_id);
        const detail = await api<{ result: Scan }>(`/api/scans/${latest.scan_id}`).catch(() => null);
        if (detail) setActiveScan(detail.result);
      } else {
        setActiveScan(null);
        setSelectedScanId("");
      }
    }
    setDataLoadedAt(new Date().toISOString());
    if (!quiet) setLoading(false);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  const analysisScanId = activeScan && activeScan.drifted > 0 ? activeScan.scan_id : "";

  useEffect(() => {
    const scanId = analysisScanId;
    if (!scanId) return undefined;
    let cancelled = false;
    let timer: number;
    const poll = async () => {
      setAiLoading(true);
      try {
        const record = await api<AIAnalysis>(`/api/scans/${scanId}/analysis`);
        if (cancelled) return;
        setAiAnalysis(record);
        setAiError("");
        if (record.status === "pending") {
          timer = window.setTimeout(() => void poll(), 1500);
        } else {
          setAiLoading(false);
        }
      } catch (reason) {
        if (cancelled) return;
        setAiError(reason instanceof Error ? reason.message : "AI analysis status could not be loaded");
        setAiLoading(false);
      }
    };
    timer = window.setTimeout(() => void poll(), 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [analysisScanId, aiRefreshKey]);

  const latestScan = useMemo(() => history[0] ?? null, [history]);
  const fleetById = useMemo(() => new Map(fleetStates.map((item) => [item.server_id, item])), [fleetStates]);
  const pendingCount = activeScan?.pending_baseline ?? servers.filter((server) => !server.current_baseline_id && server.status !== "decommissioned").length;
  const scanServers = activeScan?.servers ?? [];
  const visibleAiAnalysis = aiAnalysis?.scan_id === activeScan?.scan_id ? aiAnalysis : null;
  const openScan = async (scanId: string) => {
    setSelectedScanId(scanId);
    try {
      const detail = await api<{ result: Scan }>(`/api/scans/${scanId}`);
      setActiveScan(detail.result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scan details could not be loaded");
    }
  };

  const runScan = async (announce = true) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const scan = await api<Scan>("/api/scans", { method: "POST" });
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      await refresh(true);
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      if (announce) setNotice(`Scan ${scan.status}: ${scan.drifted} drifted, ${scan.pending_baseline} awaiting baseline approval, ${scan.failed} collection failures.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Scan could not be completed");
    } finally {
      setBusy(false);
    }
  };

  const retryAiAnalysis = async () => {
    if (!activeScan || busy || aiLoading) return;
    setAiLoading(true);
    setAiError("");
    try {
      const record = await api<AIAnalysis>(`/api/scans/${activeScan.scan_id}/analysis`, {
        method: "POST",
      });
      setAiAnalysis(record);
      setAiRefreshKey((key) => key + 1);
    } catch (reason) {
      setAiError(reason instanceof Error ? reason.message : "AI analysis request failed");
      setAiLoading(false);
    }
  };

  const approve = async (server: InventoryServer, baseline: Baseline) => {
    if (!actor.trim()) {
      setError("Enter an audit actor before approving a baseline.");
      return;
    }
    const confirmed = window.confirm(`Approve baseline v${baseline.version} for ${server.display_name} (${server.server_id})? This becomes the trusted reference for this server.`);
    if (!confirmed) return;
    setOperation({ serverId: server.server_id, kind: "approve" });
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api(`/api/servers/${encodeURIComponent(server.server_id)}/baselines/${baseline.baseline_id}/approve`, {
        method: "POST",
        body: JSON.stringify({ confirm: true, approved_by: actor.trim(), reason: "Approved in MVP demonstration" }),
      });
      setNotice(`Baseline v${baseline.version} approved for ${server.display_name}. The actor is audit metadata only.`);
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Baseline approval failed");
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  const performDemoAction = async (server: InventoryServer, kind: "drift" | "reset") => {
    if (!readiness?.demo_controls_enabled) return;
    const message = kind === "drift"
      ? `Apply ${scenario.replaceAll("_", " ")} to ${server.display_name}? This changes its simulated configuration.`
      : `Reset ${server.display_name} to its original configuration?`;
    if (!window.confirm(message)) return;
    setOperation({ serverId: server.server_id, kind });
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "drift") {
        await api<{ status: string }>(`/api/fleet/${encodeURIComponent(server.server_id)}/simulate-drift`, {
          method: "POST", body: JSON.stringify({ scenario }),
        })
      } else {
        await api<{ status: string; verified: boolean }>(`/api/fleet/${encodeURIComponent(server.server_id)}/reset`, { method: "POST" });
      }
      setNotice(kind === "drift"
        ? `Monitor confirmed ${scenario.replaceAll("_", " ")} on ${server.display_name}. Running a scan…`
        : `Monitor confirmed reset and verified ${server.display_name}. Running a scan…`);
      const scan = await api<Scan>("/api/scans", { method: "POST" });
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      await refresh(true);
      setActiveScan(scan);
      setSelectedScanId(scan.scan_id);
      setNotice(`${kind === "drift" ? "Drift detected" : "Reset verified"}: scan ${scan.status}, ${scan.drifted} drifted server(s).`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : `Demo ${kind} action failed`);
    } finally {
      setBusy(false);
      setOperation(null);
    }
  };

  const openProvisionForm = async () => {
    setError("");
    setNotice("");
    try {
      const response = await api<{ items: ServerTemplate[] }>("/api/server-templates");
      setTemplates(response.items);
      setProvisionFormOpen(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Server templates could not be loaded");
    }
  };

  const templatesForRole = templates.filter((template) => template.role === provisionRole);
  const selectedTemplate = templatesForRole.find((template) => template.template_id === provisionTemplateId) ?? templatesForRole[0];

  const createServer = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const serverId = provisionServerId.trim();
    const displayName = provisionDisplayName.trim();
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(serverId)) {
      setError("Server ID must use lowercase letters, numbers, and single hyphens.");
      return;
    }
    if (servers.some((server) => server.server_id === serverId)) {
      setError("That server ID is already registered. Choose a unique ID.");
      return;
    }
    if (!displayName || !/^[A-Za-z0-9][A-Za-z0-9 ._-]*$/.test(displayName)) {
      setError("Enter a display name using letters, numbers, spaces, periods, underscores, or hyphens.");
      return;
    }
    if (!selectedTemplate) {
      setError("Choose a supported template for this server role.");
      return;
    }

    const requestId = provisionRequestId || createRequestId();
    setProvisionRequestId(requestId);
    const body: ProvisionServerRequest = {
      request_id: requestId,
      server_id: serverId,
      display_name: displayName,
      role: provisionRole,
      template_id: selectedTemplate.template_id,
      description: provisionDescription.trim() || undefined,
      demo_mode: true,
    };
    setProvisioning(true);
    setBusy(true);
    setError("");
    setNotice(`Creating ${displayName}: reserving identity, starting its container, checking health, and collecting its initial state…`);
    let requestPending = true;
    void (async () => {
      while (requestPending) {
        await new Promise((resolve) => window.setTimeout(resolve, 1800));
        if (!requestPending) break;
        try {
          const server = await api<InventoryServer>(`/api/servers/${encodeURIComponent(serverId)}`);
          if (!requestPending) break;
          const status = server.provisioning_status ?? server.status;
          const progress: Record<string, string> = {
            provisioning: "The identity is reserved; the Provisioner is creating the server container…",
            starting: "The server container was created and is starting…",
            healthy: "Container health check passed; verifying server identity and collecting initial state…",
            registered: "Initial state was collected; creating the pending baseline candidate…",
            pending_baseline: "Server is ready. Its first baseline is pending your approval.",
            failed: `Provisioning failed during ${server.error_step ?? "an unknown step"}: ${server.provisioning_error ?? "see Monitor logs"}`,
            unavailable: "The server is registered but currently unreachable.",
          };
          setNotice(progress[status] ?? `Provisioning status: ${status}…`);
        } catch {
          if (requestPending) {
            setNotice("Waiting for the Monitor to reserve this server ID and report provisioning progress…");
          }
        }
      }
    })();
    try {
      const result = await api<ProvisioningResult>("/api/servers/provision", {
        method: "POST",
        body: JSON.stringify(body),
      });
      requestPending = false;
      setProvisionFormOpen(false);
      setProvisionServerId("");
      setProvisionDisplayName("");
      setProvisionDescription("");
      setProvisionRequestId("");
      await refresh(true);
      setNotice(`${result.display_name} is ${result.health_status}; its first configuration is Pending Approval. Approve it in Baseline enrollment before it is trusted.`);
    } catch (reason) {
      requestPending = false;
      setNotice("");
      await refresh(true);
      setError(reason instanceof Error ? reason.message : "Server provisioning failed");
    } finally {
      setBusy(false);
      setProvisioning(false);
    }
  };

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <a className="brand" href="#top" aria-label="SentinelOps overview"><span className="brand-mark"><Shield size={20} /></span><span><b>SentinelOps</b><small>CONFIGURATION MONITOR</small></span></a>
        <div className="topbar-right">
          <span className={`connection ${readiness?.status === "ready" ? "is-up" : "is-down"}`}><span className="pulse-dot" />{readiness?.status === "ready" ? "Monitor connected" : "Monitor unavailable"}</span>
          <button className="icon-button" onClick={() => void refresh()} disabled={loading || busy} aria-label="Refresh dashboard"><RefreshCw size={16} className={loading ? "spin" : ""} /></button>
        </div>
      </header>

      <div className="content" id="top">
        <section className="hero-row">
          <div><div className="eyebrow"><Activity size={14} /> FLEET OPERATIONS <span className="live-tag">LIVE API DATA</span></div><h1>Configuration posture</h1><p>Track approved server state, assess drift, and walk through a controlled demo.</p></div>
          <button className="primary-button" onClick={() => void runScan()} disabled={busy || !readiness}><Play size={15} fill="currentColor" />{busy ? "Working…" : "Run scan"}</button>
        </section>

        {(error || notice) && <div className={`banner ${error ? "banner-error" : "banner-notice"}`} role="status">{error ? <AlertTriangle size={17} /> : <Check size={17} />}<span>{error || notice}</span>{error && <button onClick={() => setError("")} aria-label="Dismiss error">×</button>}</div>}

        <section className="metric-grid" aria-label="Latest scan summary">
          <Metric label="Registered servers" value={servers.length} tone="tone-blue" icon={Server} />
          <Metric label="Clean" value={activeScan ? activeScan.servers.filter((item) => item.outcome === "clean").length : "—"} tone="tone-green" icon={Shield} />
          <Metric label="Drifted" value={activeScan?.drifted ?? "—"} tone="tone-red" icon={ShieldAlert} />
          <Metric label="Pending baseline" value={pendingCount} tone="tone-amber" icon={CircleHelp} />
          <Metric label="Unable to assess" value={activeScan?.unable_to_assess ?? activeScan?.failed ?? "—"} tone="tone-slate" icon={WifiOff} />
        </section>

        <section className="overview-strip">
          <div className="overview-item"><span className="overview-icon green"><Activity size={16} /></span><div><small>Application health</small><strong>{health?.status ?? "Unknown"}</strong><span>{health?.dependencies_checked ? "Dependencies checked" : "Process status only"}</span></div></div>
          <div className="overview-item"><span className="overview-icon blue"><Database size={16} /></span><div><small>Monitor readiness</small><strong>{readiness?.status ?? "Unknown"}</strong><span>{readiness?.database === "ready" ? "SQLite available" : "Database not confirmed"}</span></div></div>
          <div className="overview-item"><span className="overview-icon violet"><Clock3 size={16} /></span><div><small>Latest scan</small><strong>{latestScan ? formatDate(latestScan.completed_at) : "No saved scans"}</strong><span>{latestScan ? `${latestScan.status} · ${latestScan.scan_id.slice(0, 8)}` : "Run a scan to create history"}</span></div></div>
          <div className="overview-item"><span className="overview-icon amber"><Wifi size={16} /></span><div><small>Last dashboard refresh</small><strong>{dataLoadedAt ? formatDate(dataLoadedAt) : "Loading"}</strong><span>{readiness?.demo_controls_enabled ? "Demo controls enabled" : "Demo controls disabled"}</span></div></div>
        </section>

        <section className="section-card inventory-card">
          <div className="section-heading"><div><div className="section-kicker">FLEET</div><h2>Server inventory</h2><p>Each server is compared with its own explicitly approved baseline.</p></div><div style={{ display: "flex", alignItems: "center", gap: 8 }}><span className="count-chip">{servers.length} servers</span><button className="primary-button" onClick={() => void openProvisionForm()} disabled={busy}><Server size={14} />Add server</button></div></div>
          {provisionFormOpen && <form className="provision-form" onSubmit={(event) => void createServer(event)}>
            <div className="candidate-top"><div><b>Add a simulated server</b><span>Creates a real private fleet container from a validated template.</span></div><button type="button" className="icon-button" onClick={() => setProvisionFormOpen(false)} disabled={provisioning} aria-label="Close form">×</button></div>
            <div className="provision-fields">
              <label>Server ID<input required pattern="[a-z0-9]+(-[a-z0-9]+)*" maxLength={63} value={provisionServerId} onChange={(event) => setProvisionServerId(event.target.value)} placeholder="checkout-worker-04" autoComplete="off" disabled={Boolean(provisionRequestId)} /></label>
              <label>Display name<input required maxLength={128} value={provisionDisplayName} onChange={(event) => setProvisionDisplayName(event.target.value)} placeholder="Checkout Worker 04" disabled={Boolean(provisionRequestId)} /></label>
              <label>Role<select value={provisionRole} onChange={(event) => {
                const role = event.target.value as ServerTemplate["role"];
                setProvisionRole(role);
                const compatible = templates.find((template) => template.role === role);
                if (compatible) setProvisionTemplateId(compatible.template_id);
              }} disabled={Boolean(provisionRequestId)}><option value="web">Web server</option><option value="application">Application server</option><option value="database">Database server</option></select></label>
              <label>Configuration template<select value={selectedTemplate?.template_id ?? ""} onChange={(event) => setProvisionTemplateId(event.target.value as ServerTemplate["template_id"])} disabled={Boolean(provisionRequestId)}>{templatesForRole.map((template) => <option key={template.template_id} value={template.template_id}>{template.template_id} · v{template.version}</option>)}</select></label>
              <label className="provision-description">Description <span>(optional)</span><input maxLength={500} value={provisionDescription} onChange={(event) => setProvisionDescription(event.target.value)} placeholder="Purpose or owner note" disabled={Boolean(provisionRequestId)} /></label>
            </div>
            {selectedTemplate && <div className="provision-preview"><div><b>Configuration preview</b><span>Validated template · no credentials or user-supplied runtime settings</span></div><pre>{JSON.stringify({ service: selectedTemplate.service, config_version: selectedTemplate.config_version, config: selectedTemplate.config, environment: selectedTemplate.environment, demo_mode: true }, null, 2)}</pre></div>}
            <div className="provision-actions"><span>First collection creates a pending candidate. Baseline approval stays explicit.</span><button className="primary-button" type="submit" disabled={busy || !selectedTemplate}>{provisioning ? <><RefreshCw size={14} className="spin" />Provisioning…</> : "Create server"}</button></div>
          </form>}
          {loading && servers.length === 0 ? <div className="empty-state"><RefreshCw size={19} className="spin" />Loading server inventory…</div> : servers.length === 0 ? <div className="empty-state">No registered servers were returned by the Monitor.</div> : (
            <div className="table-wrap"><table><thead><tr><th>SERVER</th><th>ROLE</th><th>ENROLLMENT</th><th>COLLECTION</th><th>BASELINE</th><th>OBSERVED FINGERPRINT</th><th>DETAILS</th></tr></thead><tbody>
              {servers.map((server) => {
                const collected = fleetById.get(server.server_id);
                const assessment = activeScan?.servers.find((item) => item.server_id === server.server_id);
                return <tr key={server.server_id}>
                  <td><div className="server-name"><span className="server-avatar"><Server size={15} /></span><span><b>{server.display_name}</b><small>{server.server_id}</small></span></div></td>
                  <td><span className="role-label">{server.role ?? "Unknown"}</span></td>
                  <td><StatusPill value={server.status} /></td>
                  <td><StatusPill value={collected?.status === "success" ? "reachable" : collected?.status === "failed" ? "unreachable" : server.reachability} /></td>
                  <td>{server.current_baseline_version ? <span className="baseline-version">v{server.current_baseline_version}</span> : <span className="muted">Awaiting approval</span>}</td>
                  <td><span className="fingerprint"><Fingerprint size={13} />{shortHash(collected?.fingerprint)}</span></td>
                  <td><details className="server-details"><summary>View</summary><div><span>Template <b>{server.template_id ? `${server.template_id} · v${server.template_version}` : "Compose-managed"}</b></span><span>Container <b>{server.container_name ?? "Managed by Compose"}</b></span><span>Health <b>{server.health_status ?? server.reachability}</b></span><span>Last scan <b>{assessment?.outcome ?? "Not assessed"}</b></span>{assessment?.findings.map((finding, index) => <span key={`${finding.evidence.field_path}-${index}`}>Finding <b>{finding.evidence.field_path} · {finding.assessment.severity}</b></span>)}{server.provisioning_error && <span className="provision-error">Provisioning failed at {server.error_step}: {server.provisioning_error}</span>}{server.description && <span>Description <b>{server.description}</b></span>}</div></details></td>
                </tr>;
              })}
            </tbody></table></div>
          )}
          <div className="table-foot"><span>Latest successful collection state from the Monitor</span><span>Refresh: {dataLoadedAt ? new Date(dataLoadedAt).toLocaleTimeString() : "—"}</span></div>
        </section>

        <section className="two-column">
          <div className="section-card enrollment-card">
            <div className="section-heading compact"><div><div className="section-kicker">TRUSTED CONFIGURATION</div><h2>Baseline enrollment</h2><p>Review and approve each server independently.</p></div><span className="count-chip amber-chip">{Object.values(candidates).flat().length} pending</span></div>
            <div className="actor-field"><label htmlFor="audit-actor">Approval actor <span>audit metadata only</span></label><input id="audit-actor" value={actor} maxLength={128} onChange={(event) => setActor(event.target.value)} placeholder="operator name" /></div>
            <div className="disclosure"><AlertTriangle size={15} /><span>There is no real authentication in this MVP. This name records who performed the demo action; it does not authorize the request.</span></div>
            <div className="candidate-list">
              {servers.flatMap((server) => (candidates[server.server_id] ?? []).map((candidate) => <article className="candidate" key={candidate.baseline_id}>
                <div className="candidate-top"><div><b>{server.display_name}</b><span>{server.server_id} · candidate v{candidate.version}</span></div><StatusPill value="pending" /></div>
                <div className="candidate-meta"><span>Service group <b>{candidate.snapshot.service_group}</b></span><span>Created {formatDate(candidate.created_at)}</span></div>
                <div className="snapshot-preview"><span>CONFIG</span>{Object.entries(candidate.snapshot.config).map(([key, value]) => <code key={key}>{key}: {String(value)}</code>)}<span>ENVIRONMENT</span>{Object.entries(candidate.snapshot.environment).map(([key, value]) => <code key={key}>{key}: {String(value)}</code>)}</div>
                <div className="candidate-bottom"><span className="fingerprint"><Fingerprint size={13} />{shortHash(candidate.fingerprint)}</span><button className="approve-button" disabled={busy || !actor.trim()} onClick={() => void approve(server, candidate)}>{operation?.serverId === server.server_id && operation.kind === "approve" ? <RefreshCw size={14} className="spin" /> : <Check size={14} />}Approve baseline</button></div>
              </article>))}
              {Object.values(candidates).flat().length === 0 && <div className="empty-inline"><Check size={17} />No pending candidate. New server state is never approved automatically.</div>}
            </div>
          </div>

          <div className="section-card demo-card">
            <div className="section-heading compact"><div><div className="section-kicker">DEMO ONLY</div><h2>Drift controls</h2><p>Change a simulated server, scan it, then restore it.</p></div><span className={`mode-chip ${readiness?.demo_controls_enabled ? "mode-on" : "mode-off"}`}>{readiness?.demo_controls_enabled ? "ENABLED" : "DISABLED"}</span></div>
            <div className="demo-warning"><ShieldAlert size={18} /><div><b>Predefined scenarios only</b><span>These actions call the server&apos;s allowlisted demo endpoints. No arbitrary commands are accepted.</span></div></div>
            <label className="select-label" htmlFor="scenario">Drift scenario</label>
            <div className="select-wrap"><select id="scenario" value={scenario} onChange={(event) => setScenario(event.target.value)} disabled={!readiness?.demo_controls_enabled || busy}><option value="security_drift">Security drift · debug mode</option><option value="performance_drift">Performance drift · connection pool</option></select><ChevronDown size={15} /></div>
            <div className="demo-server-list">{servers.map((server) => <div className="demo-server" key={server.server_id}><div className="demo-server-id"><span className="server-avatar"><Server size={14} /></span><span><b>{server.display_name}</b><small>{server.server_id} · {server.current_baseline_id ? `baseline v${server.current_baseline_version}` : "baseline required for assessment"}</small></span></div><div className="demo-actions"><button className="drift-button" disabled={!readiness?.demo_controls_enabled || busy} onClick={() => void performDemoAction(server, "drift")} title={readiness?.demo_controls_enabled ? "Apply predefined demo drift" : "Enable DEMO_MODE on the Monitor and server containers"}>{operation?.serverId === server.server_id && operation.kind === "drift" ? <RefreshCw size={14} className="spin" /> : <AlertTriangle size={14} />}Drift</button><button className="reset-button" disabled={!readiness?.demo_controls_enabled || busy} onClick={() => void performDemoAction(server, "reset")} title="Reset and verify original state">{operation?.serverId === server.server_id && operation.kind === "reset" ? <RefreshCw size={14} className="spin" /> : <RotateCcw size={14} />}Reset</button></div></div>)}</div>
            {!readiness?.demo_controls_enabled && <div className="empty-inline"><CircleHelp size={16} />Demo controls are disabled by the Monitor configuration.</div>}
          </div>
        </section>

        <section className="two-column history-grid">
          <div className="section-card history-card">
            <div className="section-heading compact"><div><div className="section-kicker">PERSISTED RESULTS</div><h2>Scan history</h2><p>Saved assessments include the baseline version used.</p></div><span className="count-chip">{history.length} recent</span></div>
            {history.length === 0 ? <div className="empty-state">No persisted scans yet. Run a scan to begin.</div> : <div className="history-list">{history.map((scan) => <button className={`history-row ${selectedScanId === scan.scan_id ? "selected" : ""}`} key={scan.scan_id} onClick={() => void openScan(scan.scan_id)}><span className="history-state"><span className={`result-dot ${scan.drifted ? "dot-red" : scan.failed ? "dot-amber" : "dot-green"}`} /><span><b>{formatDate(scan.completed_at)}</b><small>{scan.scan_id.slice(0, 13)}</small></span></span><span className="history-counts"><b className={scan.drifted ? "text-red" : ""}>{scan.drifted} drift</b><span>{scan.pending_baseline} pending · {scan.failed} failed</span></span><StatusPill value={scan.status} /></button>)}</div>}
          </div>

          <div className="section-card findings-card">
            <div className="section-heading compact"><div><div className="section-kicker">DETERMINISTIC EVIDENCE</div><h2>Assessment details</h2><p>{selectedScanId ? `Scan ${selectedScanId.slice(0, 13)}` : "Select a saved scan"}</p></div><span className="count-chip">{scanServers.length} assessed</span></div>
            {!activeScan ? <div className="empty-state"><ArrowDownToLine size={17} />Run or select a scan to view its findings.</div> : <div className="assessment-list">{activeScan.servers.map((assessment) => <details className="assessment" key={`${activeScan.scan_id}-${assessment.server_id}`} open={assessment.outcome === "drifted"}>
              <summary><span className="assessment-title"><span className={`result-dot ${assessment.outcome === "drifted" ? "dot-red" : assessment.outcome === "clean" ? "dot-green" : "dot-amber"}`} /><b>{servers.find((item) => item.server_id === assessment.server_id)?.display_name ?? assessment.server_id}</b><small>{assessment.server_id}</small></span><span className="assessment-provenance">{assessment.baseline_version ? `baseline v${assessment.baseline_version}` : "No approved baseline"}</span><StatusPill value={assessment.outcome} /></summary>
              <div className="assessment-body">
                {assessment.collection_error && <div className="collection-error"><WifiOff size={15} /><span>Collection failed ({assessment.collection_error.kind}): {assessment.collection_error.message}</span></div>}
                {assessment.assessment_error && <div className="collection-error"><AlertTriangle size={15} /><span>{assessment.assessment_error}</span></div>}
                {assessment.findings.map((finding, index) => <div className="finding" key={`${finding.evidence.field_path}-${index}`}><div className="finding-heading"><code>{finding.evidence.field_path}</code><span className={`severity severity-${finding.assessment.severity.toLowerCase()}`}>{finding.assessment.severity}</span></div><div className="evidence-pair"><span><small>EXPECTED</small><code>{describeValue(finding.evidence.expected)}</code></span><span><small>OBSERVED</small><code>{describeValue(finding.evidence.actual)}</code></span></div>{finding.assessment.policy_ids.length > 0 && <div className="policy-ref">Policy: {finding.assessment.policy_ids.join(", ")}</div>}</div>)}
                {assessment.policy_violations.map((violation, index) => <div className="finding policy-finding" key={`${violation.policy_id}-${index}`}><div className="finding-heading"><code>{violation.field_path}</code><span className={`severity severity-${violation.severity.toLowerCase()}`}>{violation.severity}</span></div><p>Policy violation: {violation.policy_id}</p><div className="evidence-pair"><span><small>OBSERVED</small><code>{describeValue(violation.observed)}</code></span></div></div>)}
                {assessment.outcome === "clean" && <div className="clean-note"><Check size={15} />Configuration matches approved baseline.</div>}
                {assessment.outcome === "pending_baseline" && <div className="empty-inline"><CircleHelp size={15} />Assessment is pending explicit baseline approval.</div>}
                {assessment.findings.length === 0 && assessment.policy_violations.length === 0 && assessment.outcome === "unable_to_assess" && !assessment.collection_error && <div className="empty-inline"><CircleHelp size={15} />No assessment evidence is available.</div>}
              </div>
            </details>)}</div>}
          </div>
        </section>

        {activeScan && activeScan.drifted > 0 && <section className="section-card ai-analysis-card">
          <div className="section-heading"><div><div className="section-kicker"><Sparkles size={13} /> ADVISORY EXPLANATION</div><h2>AI Drift Analysis</h2><p>AI explains verified findings; deterministic detection and severity remain authoritative.</p></div><span className={`ai-state ai-${visibleAiAnalysis?.status ?? (aiError ? "failed" : "pending")}`}>{visibleAiAnalysis?.status ?? (aiError ? "unavailable" : "loading")}</span></div>
          {aiLoading && <div className="ai-progress"><RefreshCw size={16} className="spin" /> Requesting or retrieving the saved explanation…</div>}
          {aiError && <div className="ai-message ai-failed"><AlertTriangle size={16} /><span>{aiError}</span></div>}
          {visibleAiAnalysis?.status === "pending" && !aiError && <div className="ai-message"><RefreshCw size={16} className="spin" /><span>Analysis is pending. Scan results are already saved and available.</span></div>}
          {visibleAiAnalysis?.status === "disabled" && <div className="ai-message"><CircleHelp size={16} /><span>{visibleAiAnalysis.error_summary ?? "AI analysis is disabled. Enable AI_ENABLED and restart the Monitor to request an explanation."}</span></div>}
          {visibleAiAnalysis?.status === "failed" && <div className="ai-message ai-failed"><AlertTriangle size={16} /><span>{visibleAiAnalysis.error_summary ?? "The provider did not return an explanation."}</span></div>}
          {visibleAiAnalysis?.status === "completed" && visibleAiAnalysis.explanation && <>
            {visibleAiAnalysis.explanation.format === "text" && <div className="ai-message"><CircleHelp size={16} /><span>The provider returned unstructured text; impact and recommendations could not be separately validated.</span></div>}
            <div className="ai-meta">Model: <b>{visibleAiAnalysis.model ?? "unknown"}</b> · Provider: <b>{visibleAiAnalysis.provider}</b> · Servers: <b>{visibleAiAnalysis.server_ids.join(", ")}</b></div>
            <div className="ai-sections">
              <article><h3>What happened</h3><p>{visibleAiAnalysis.explanation.summary}</p></article>
              <article><h3>Security / Operational Impact</h3><p>{visibleAiAnalysis.explanation.impact}</p></article>
              <article><h3>Recommended Fix</h3>{visibleAiAnalysis.explanation.remediation.length ? <ul>{visibleAiAnalysis.explanation.remediation.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <p className="muted">The provider did not return structured remediation steps.</p>}</article>
              <article><h3>Prevention</h3>{visibleAiAnalysis.explanation.prevention.length ? <ul>{visibleAiAnalysis.explanation.prevention.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <p className="muted">The provider did not return structured prevention steps.</p>}</article>
            </div>
          </>}
          {(aiError || visibleAiAnalysis?.status === "failed" || visibleAiAnalysis?.provider === "local_demo") && <button className="retry-ai-button" disabled={aiLoading || busy} onClick={() => void retryAiAnalysis()}><RefreshCw size={14} />{visibleAiAnalysis?.provider === "local_demo" ? "Try Ollama again" : "Retry AI analysis"}</button>}
          {visibleAiAnalysis?.status === "disabled" && <div className="ai-footnote">No request was sent to the AI provider. Configure AI_ENABLED=true to enable advisory analysis.</div>}
        </section>}

        <footer className="footer-note"><span><Database size={14} /> Data is returned by the live Monitor API and saved to SQLite.</span><span>Audit actors are not authenticated · No automatic remediation</span></footer>
      </div>
    </main>
  );
}
