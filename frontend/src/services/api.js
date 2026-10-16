const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { Accept: "application/json", ...options.headers },
    });
  } catch {
    throw new Error("Could not reach the API. Confirm the backend is running and the Vite proxy is configured.");
  }

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `API request failed (${response.status}).`);
  }
  return body;
}

export const getHealth = () => request("/api/health");
export const getFleet = () => request("/api/fleet/states");
export const runFleetScan = () => request("/api/scans", { method: "POST" });

export const simulateDrift = (serverId) =>
  request(`/api/fleet/${encodeURIComponent(serverId)}/simulate-drift`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario: "security_drift" }),
  });

export const resetServer = (serverId) =>
  request(`/api/fleet/${encodeURIComponent(serverId)}/reset`, { method: "POST" });
