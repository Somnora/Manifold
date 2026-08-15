"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Cluster, type ClusterNode, type InstanceTypeInfo, type Region, type Filesystem } from "@/lib/api";
import { ModalPortal } from "@/components/ModalPortal";
import { usePolling } from "@/lib/usePolling";
import { motion, AnimatePresence } from "framer-motion";
import { Terminal, Server, Cpu, Database, Network, Power, RefreshCw, Layers } from "lucide-react";
import { useTerminalDock } from "@/components/TerminalDock";

export function ClusterPanel() {
  const { data: clusters, error, refresh } = usePolling(api.clusters, 5000);
  const [showLaunchModal, setShowLaunchModal] = useState(false);
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [selectedCluster, setSelectedCluster] = useState<Cluster | null>(null);

  const { dockInstance } = useTerminalDock();

  // Form state
  const [name, setName] = useState("ray-cluster-01");
  const [gpuType, setGpuType] = useState("gpu_8x_h100_sxm5");
  const [region, setRegion] = useState("us-east-1");
  const [filesystem, setFilesystem] = useState("manifold-data");
  const [nodeCount, setNodeCount] = useState(2);
  const [launching, setLaunching] = useState(false);
  const [formErr, setFormErr] = useState("");
  const [termBusy, setTermBusy] = useState<string | null>(null);
  // Set when termination was REFUSED: a node's data-rescue could not save
  // every file, so the cluster kept running. Holds the per-node reasons.
  const [termBlocked, setTermBlocked] = useState<{
    clusterId: string;
    errors: string[];
  } | null>(null);

  useEffect(() => {
    api.instanceTypes().then(setTypes).catch(() => {});
    api.regions().then((r) => {
      setRegions(r);
      if (r.length > 0 && !region) setRegion(r[0].code);
    }).catch(() => {});
    api.filesystems().then((fs) => {
      setFilesystems(fs);
      if (fs.length > 0) setFilesystem(fs[0].name);
    }).catch(() => {});
  }, []);

  async function handleLaunch(e: React.FormEvent) {
    e.preventDefault();
    setLaunching(true);
    setFormErr("");
    try {
      await api.launchCluster({
        name,
        instance_type: gpuType,
        region,
        filesystem,
        node_count: nodeCount,
      });
      setShowLaunchModal(false);
      refresh();
    } catch (err) {
      setFormErr(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLaunching(false);
    }
  }

  // Termination saves each node's scratch files first (per the data-safety
  // policy), then stops the billing. It only refuses if a file could NOT be
  // saved — and then it says which node and why. force=true is the explicit
  // "lose the files" override, only offered after a blocked rescue.
  async function handleTerminate(clusterId: string, force = false) {
    const retrying = termBlocked?.clusterId === clusterId;
    if (!force && !retrying && !confirm(
      "Terminate this cluster? Each node's unsaved files are rescued first; termination stops if a file can't be saved.",
    )) return;
    setTermBusy(clusterId);
    try {
      const res = await api.terminateCluster(clusterId, force);
      if (res.terminated) {
        setTermBlocked(null);
      } else {
        const errors = res.reports
          .filter((r) => r.error)
          .map((r) => `${r.instance_id ?? "node"}: ${r.error}`);
        setTermBlocked({
          clusterId,
          errors: errors.length ? errors : ["A node could not be terminated safely."],
        });
      }
      refresh();
    } catch (err) {
      alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setTermBusy(null);
    }
  }

  const getBurnRate = (c: Cluster) => {
    const typeInfo = types[c.gpu_type];
    if (!typeInfo) return 0;
    return typeInfo.price_usd_per_hour * c.node_count;
  };

  // The head node's REAL cloud instance id, if it has booted. This — not
  // c.head_instance_id (a launch id) — is what the dock terminal needs to
  // open an SSH session. Null while the head is still provisioning.
  const headRealId = (c: Cluster): string | null => {
    const head = c.nodes?.find((n) => n.role === "head");
    return head?.lambda_instance_id ?? null;
  };

  return (
    <div className="relative overflow-hidden rounded-xl border border-zinc-200 bg-white/80 p-6 shadow-sm backdrop-blur-md">
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-emerald-500/40 to-transparent" />
      <div className="flex items-center justify-between border-b border-zinc-200 pb-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900 flex items-center gap-2">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
            </span>
            Elastic GPU Clusters
          </h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Manage multi-node Ray, vLLM, and DeepSpeed GPU swarms
          </p>
        </div>
        <button
          onClick={() => setShowLaunchModal(true)}
          className="rounded-lg bg-emerald-600 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 transition-colors"
        >
          + Launch Swarm
        </button>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800">
          Failed to load clusters: {error}
        </div>
      )}

      {/* Cluster List */}
      <div className="mt-4 space-y-4">
        {!clusters || clusters.length === 0 ? (
          <p className="rounded-lg border border-dashed border-zinc-300 bg-zinc-50 px-4 py-3 text-xs text-zinc-500">
            No active GPU clusters. Launch a multi-node swarm to begin
            distributed inference or fine-tuning.
          </p>
        ) : (
          clusters.map((c) => (
            <motion.div
              key={c.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-xl border border-zinc-200 bg-zinc-50 p-5 transition-all hover:border-zinc-300 hover:shadow-md"
            >
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div className="space-y-2">
                  <div className="flex items-center gap-3">
                    <span className="font-semibold text-base text-zinc-900 flex items-center gap-2">
                      <Layers className="w-4 h-4 text-emerald-400" />
                      {c.name || c.id}
                    </span>
                    <span className={`rounded-full px-2.5 py-0.5 text-[10px] font-medium border flex items-center gap-1.5 ${
                      c.status === "active" ? "bg-emerald-100 text-emerald-800 border-emerald-200" :
                      c.status === "provisioning" ? "bg-amber-100 text-amber-800 border-amber-200" :
                      "bg-zinc-100 text-zinc-600 border-zinc-200"
                    }`}>
                      {c.status === "active" && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />}
                      {c.status === "provisioning" && <RefreshCw className="w-3 h-3 animate-spin" />}
                      {c.status.toUpperCase()}
                    </span>
                  </div>

                  <div className="flex flex-wrap items-center gap-4 text-xs text-zinc-500">
                    <div className="flex items-center gap-1.5 bg-white px-2 py-1 rounded-md border border-zinc-200">
                      <Cpu className="w-3.5 h-3.5 text-zinc-400" />
                      <span>{c.node_count}x <strong className="text-zinc-700">{c.gpu_type}</strong></span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Server className="w-3.5 h-3.5 text-zinc-400" />
                      <span><strong className="text-zinc-700">{c.region}</strong></span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Database className="w-3.5 h-3.5 text-zinc-400" />
                      <span><strong className="text-zinc-700">{c.filesystem}</strong></span>
                    </div>
                    {c.head_ip && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-emerald-700 font-mono bg-emerald-50 px-1.5 py-0.5 rounded border border-emerald-200">
                          {c.head_ip}
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex flex-col items-end gap-2">
                  <div className="text-xs font-mono bg-white px-2.5 py-1 rounded-md border border-zinc-200 text-zinc-700 flex items-center gap-2">
                    <span className="text-zinc-500">Burn Rate:</span>
                    <span className="text-amber-700 font-semibold">${getBurnRate(c).toFixed(2)}/hr</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {/* SSH Head opens a dock terminal to the head node's REAL
                        cloud instance id. While the head is still booting there
                        is nothing to dial, so show a provisioning chip instead
                        of docking a dead terminal. */}
                    {c.status === "active" && headRealId(c) && (
                      <button
                        onClick={() => dockInstance(headRealId(c)!, c.name || c.id)}
                        className="flex items-center gap-1.5 rounded border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-100 transition-colors"
                        title="Open SSH Terminal to Head Node"
                      >
                        <Terminal className="w-3.5 h-3.5" />
                        SSH Head
                      </button>
                    )}
                    {c.status !== "terminated" && !headRealId(c) && (
                      <span
                        className="flex items-center gap-1.5 rounded border border-zinc-200 px-2.5 py-1 text-xs text-zinc-400"
                        title="The head node is still booting; SSH will be available once it comes online"
                      >
                        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                        Head provisioning...
                      </span>
                    )}
                    <button
                      onClick={() => setSelectedCluster(selectedCluster?.id === c.id ? null : c)}
                      className={`flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs transition-colors ${
                        selectedCluster?.id === c.id
                          ? "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100"
                          : "border-zinc-300 text-zinc-700 hover:bg-zinc-100"
                      }`}
                    >
                      <Network className="w-3.5 h-3.5" />
                      {selectedCluster?.id === c.id ? "Hide Topology" : "Topology"}
                    </button>
                    {c.status !== "terminated" && (
                      <button
                        onClick={() => handleTerminate(c.id)}
                        disabled={termBusy === c.id}
                        className="flex items-center gap-1.5 rounded border border-red-200 px-2.5 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 transition-colors"
                      >
                        <Power className="w-3.5 h-3.5" />
                        {termBusy === c.id ? "Stopping..." : "Terminate"}
                      </button>
                    )}
                  </div>
                </div>
              </div>

              {/* Blocked termination: the data-rescue refused, so nothing was
                  destroyed. Same language as InstanceCard's blocked panel. */}
              {termBlocked?.clusterId === c.id && (
                <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-3">
                  <p className="text-sm font-medium text-amber-900">
                    Kept running: some files could not be saved
                  </p>
                  <p className="mt-1 text-xs text-amber-800">
                    Manifold tried to save each node&apos;s scratch files before
                    shutting the cluster down and could not. It is still billing,
                    because losing these files is permanent and an extra billing
                    hour is not.
                  </p>
                  <ul className="mt-2 max-h-40 overflow-y-auto font-mono text-xs text-amber-900 space-y-0.5">
                    {termBlocked.errors.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      onClick={() => handleTerminate(c.id)}
                      disabled={termBusy === c.id}
                      className="rounded bg-zinc-900 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50 transition-colors"
                    >
                      {termBusy === c.id ? "Saving..." : "Try saving them again, then terminate"}
                    </button>
                    <button
                      onClick={() => handleTerminate(c.id, true)}
                      disabled={termBusy === c.id}
                      className="rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 transition-colors"
                    >
                      Terminate anyway (lose unsaved files)
                    </button>
                    <button
                      onClick={() => setTermBlocked(null)}
                      disabled={termBusy === c.id}
                      className="rounded border border-zinc-300 px-3 py-1 text-xs text-zinc-700 hover:bg-zinc-100 disabled:opacity-50 transition-colors"
                    >
                      Keep running
                    </button>
                  </div>
                </div>
              )}

              {/* Node Details Expansion - Sleek Tree Topology */}
              <AnimatePresence>
                {selectedCluster?.id === c.id && c.nodes && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    className="mt-5 border-t border-zinc-200 pt-4 overflow-hidden"
                  >
                    <h4 className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-3">Cluster Topology Tree</h4>
                    <div className="relative pl-4 space-y-3">
                      {/* Vertical line connecting head to workers */}
                      <div className="absolute left-6 top-6 bottom-6 w-px bg-gradient-to-b from-zinc-300 via-zinc-200 to-transparent" />

                      {[...c.nodes].sort((a, b) => a.role === "head" ? -1 : 1).map((node: ClusterNode) => (
                        <div key={node.instance_id ?? `${c.id}-${node.node_index}`} className="relative flex items-center gap-3">
                          {/* Horizontal connector branch */}
                          {node.role === "worker" && (
                            <div className="w-4 h-px bg-zinc-300 ml-2" />
                          )}
                          <div className={`flex-1 flex items-center justify-between rounded-lg bg-white p-3 border ${
                            node.role === "head" ? "border-emerald-200" : "border-zinc-200"
                          }`}>
                            <div className="flex items-center gap-3">
                              <div className={`p-1.5 rounded-md ${node.role === "head" ? "bg-emerald-50 text-emerald-700" : "bg-sky-50 text-sky-700"}`}>
                                {node.role === "head" ? <Server className="w-4 h-4" /> : <Cpu className="w-4 h-4" />}
                              </div>
                              <div>
                                <div className="font-mono text-zinc-900 text-xs flex items-center gap-2">
                                  {node.role.toUpperCase()} #{node.node_index}
                                  <span className={`text-[9px] px-1.5 py-0.5 rounded-sm ${
                                    node.status === "active" ? "bg-emerald-100 text-emerald-800" : "bg-zinc-100 text-zinc-600"
                                  }`}>
                                    {node.status}
                                  </span>
                                </div>
                                <div className="text-zinc-500 text-[10px] mt-0.5">
                                  Instance:{" "}
                                  {node.lambda_instance_id ? (
                                    <span className="font-mono text-zinc-600">{node.lambda_instance_id}</span>
                                  ) : (
                                    <span className="text-zinc-400">provisioning...</span>
                                  )}
                                </div>
                              </div>
                            </div>
                            <div className="font-mono text-[11px] bg-zinc-100 px-2 py-1 rounded border border-zinc-200">
                              {node.ip ? (
                                <span className="text-zinc-700">{node.ip}</span>
                              ) : (
                                <span className="text-zinc-400 flex items-center gap-1.5">
                                  <RefreshCw className="w-3 h-3 animate-spin" />
                                  Assigning IP...
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          ))
        )}
      </div>

      {/* Launch Modal */}
      <AnimatePresence>
        {showLaunchModal && (
          // Through a portal to <body>: this panel is backdrop-blurred and
          // overflow-hidden, which made it the containing block for a
          // `fixed` child and then clipped it to a sliver. See ModalPortal.
          <ModalPortal
            onClose={() => setShowLaunchModal(false)}
            labelledBy="launch-swarm-title"
          >
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setShowLaunchModal(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 10 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 10 }}
              // max-h + overflow-y-auto: a tall dialog on a short window
              // must scroll ITSELF rather than run off the screen, which is
              // the other half of "I cannot reach the Launch button".
              className="relative z-10 my-auto max-h-[calc(100vh-2rem)] w-full max-w-lg overflow-y-auto rounded-2xl border border-zinc-200 bg-white p-6 shadow-xl"
            >
              <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-emerald-500 to-teal-400" />
              <h3 id="launch-swarm-title" className="text-lg font-semibold text-zinc-900 mb-1">Launch Swarm</h3>
              <p className="text-xs text-zinc-500 mb-5">Provision a high-density multi-node GPU cluster.</p>

              {formErr && (
                <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800">
                  {formErr}
                </div>
              )}

              <form onSubmit={handleLaunch} className="space-y-4 text-sm">
                <div>
                  <label className="block text-zinc-500 mb-1.5 text-xs font-medium">Cluster Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2.5 text-zinc-900 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all placeholder-zinc-400"
                    placeholder="e.g. ray-cluster-01"
                    required
                  />
                </div>

                <div>
                  <label className="block text-zinc-500 mb-1.5 text-xs font-medium">Instance GPU Type</label>
                  <select
                    value={gpuType}
                    onChange={(e) => setGpuType(e.target.value)}
                    className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2.5 text-zinc-900 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all"
                  >
                    {Object.entries(types).map(([id, info]) => (
                      <option key={id} value={id}>
                        {info.description} (${info.price_usd_per_hour}/hr)
                      </option>
                    ))}
                    {/* Names only. These options exist so the form still
                        works before the catalog answers - but they carried
                        hardcoded prices ($24.72/hr and friends), which is an
                        invented number on the screen where the user decides
                        to spend. The Est. Burn Rate below already says "rate
                        unavailable" in this same state; this list has to
                        agree with it. A price on this screen comes from the
                        provider or it does not appear. */}
                    {!Object.keys(types).length && (
                      <>
                        <option value="gpu_8x_h100_sxm5">8x H100 SXM5 (rate loading)</option>
                        <option value="gpu_8x_a100_80gb">8x A100 80GB (rate loading)</option>
                        <option value="gpu_4x_a100_80gb">4x A100 80GB (rate loading)</option>
                      </>
                    )}
                  </select>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-zinc-500 mb-1.5 text-xs font-medium">Node Count</label>
                    <input
                      type="number"
                      min={1}
                      max={128}
                      value={nodeCount}
                      onChange={(e) => setNodeCount(parseInt(e.target.value) || 1)}
                      className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2.5 text-zinc-900 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all"
                    />
                  </div>
                  <div>
                    <label className="block text-zinc-500 mb-1.5 text-xs font-medium">Region</label>
                    <select
                      value={region}
                      onChange={(e) => setRegion(e.target.value)}
                      className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2.5 text-zinc-900 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all"
                    >
                      {regions.map((r) => (
                        <option key={r.code} value={r.code}>{r.name}</option>
                      ))}
                      {!regions.length && <option value="us-east-1">US East 1</option>}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="block text-zinc-500 mb-1.5 text-xs font-medium">Persistent Storage Volume</label>
                  <select
                    value={filesystem}
                    onChange={(e) => setFilesystem(e.target.value)}
                    className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2.5 text-zinc-900 focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500 transition-all"
                  >
                    {filesystems.map((fs) => (
                      <option key={fs.name} value={fs.name}>{fs.name} ({fs.region})</option>
                    ))}
                    {!filesystems.length && <option value="manifold-data">manifold-data</option>}
                  </select>
                </div>

                <div className="pt-4 flex justify-between items-center border-t border-zinc-200">
                  <div className="text-xs text-zinc-500 flex flex-col">
                    <span>Est. Burn Rate</span>
                    {/* Catalog rate only. Never invent a number on the
                        screen where the user decides to spend: until the
                        catalog answers, say so. */}
                    {types[gpuType] ? (
                      <span className="text-emerald-700 font-mono font-medium">
                        ${(types[gpuType].price_usd_per_hour * nodeCount).toFixed(2)}/hr
                      </span>
                    ) : (
                      <span className="font-mono text-zinc-400">
                        rate unavailable
                      </span>
                    )}
                  </div>
                  <div className="flex gap-3">
                    <button
                      type="button"
                      onClick={() => setShowLaunchModal(false)}
                      className="rounded-lg px-4 py-2 text-xs font-medium text-zinc-500 hover:text-zinc-900 hover:bg-zinc-100 transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={launching}
                      className="rounded-lg bg-emerald-600 px-5 py-2 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50 transition-colors"
                    >
                      {launching ? "Provisioning..." : "Launch Swarm"}
                    </button>
                  </div>
                </div>
              </form>
            </motion.div>
          </ModalPortal>
        )}
      </AnimatePresence>
    </div>
  );
}
