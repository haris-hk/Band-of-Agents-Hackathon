"use client";

import { useMemo, useRef, useState } from "react";

type AgentEvent = {
  stage: string;
  agent: string;
  status: string;
  payload: Record<string, unknown>;
  error?: string;
  created_at?: string;
};

const sampleAlert = {
  service: "checkout-api",
  environment: "prod",
  error: "500 spike: TypeError cannot read property customer_id of null",
  impact: "8% checkout failures for paid traffic",
  deploy_sha: "abc1234",
};

export default function IncidentDashboard() {
  const [payload, setPayload] = useState(JSON.stringify(sampleAlert, null, 2));
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const final = useMemo(() => {
    const done = [...events].reverse().find((event) => event.status === "done");
    return {
      rca: (done?.payload?.rca as { final_markdown?: string } | undefined)?.final_markdown ?? "",
      fix: (done?.payload?.fix as { patch_unified_diff?: string } | undefined)?.patch_unified_diff ?? "",
    };
  }, [events]);

  function run() {
    const ws = new WebSocket("ws://localhost:8000/ws/incidents");
    wsRef.current = ws;
    setEvents([]);
    ws.onopen = () => {
      setConnected(true);
      ws.send(JSON.stringify({ alert: JSON.parse(payload) }));
    };
    ws.onmessage = (message) => setEvents((current) => [...current, JSON.parse(message.data)]);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
  }

  return (
    <main className="dashboard">
      <section className="console">
        <div className="toolbar">
          <h1>Incident Console</h1>
          <button onClick={run}>
            {connected ? "Running" : "Run"}
          </button>
        </div>
        <textarea
          className="payloadInput"
          value={payload}
          onChange={(event) => setPayload(event.target.value)}
        />
      </section>

      <section className="workspace">
        <div className="panel">
          <h2>Live Execution Feed</h2>
          <div className="feed">
            {events.map((event, index) => (
              <div key={index} className="event">
                <b>{event.status}</b> {event.agent} / {event.stage}
                {event.error ? <pre className="error">{event.error}</pre> : null}
                <pre>{JSON.stringify(event.payload, null, 2)}</pre>
              </div>
            ))}
          </div>
        </div>

        <div className="results">
          <div className="panel">
            <h2>RCA</h2>
            <pre className="resultText">{final.rca}</pre>
          </div>
          <div className="panel">
            <h2>Proposed Fix</h2>
            <pre className="codeText">{final.fix}</pre>
          </div>
        </div>
      </section>
    </main>
  );
}
