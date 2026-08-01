"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Cluster, type ClusterNode, type InstanceTypeInfo, type Region, type Filesystem } from "@/lib/api";
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

  async function handleTerminate(clusterId: string) {
    if (!confirm("Are you sure you want to terminate this cluster? All nodes will be safely stopped.")) return;
    setTermBusy(clusterId);
    try {
      await api.terminateCluster(clusterId, true);
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

  return (
    <div className="rounded-xl border border-zinc-800/60 bg-zinc-950/80 backdrop-blur-md p-6 shadow-2xl text-zinc-100 relative overflow-hidden">
      <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-emerald-500/20 to-transparent" />
      <div className="flex items-center justify-between border-b border-zinc-800/50 pb-4">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-100 flex items-center gap-2">
            <span className="relative flex h-3 w-3">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
            </span>
            Elastic GPU Clusters
          </h2>
          <p className="text-xs text-zinc-400 mt-0.5">
            Manage multi-node Ray, vLLM, and DeepSpeed GPU swarms
          </p>
        </div>
        <button
          onClick={() => setShowLaunchModal(true)}
          className="rounded-lg bg-emerald-600/90 backdrop-blur px-3.5 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 transition-all shadow-lg shadow-emerald-900/40 ring-1 ring-emerald-500/30"
        >
          + Launch Swarm
        </button>
      </div>

      {error && (
        <div className="mt-4 rounded-lg bg-red-950/50 border border-red-800 p-3 text-xs text-red-300 backdrop-blur-sm">
          Failed to load clusters: {error}
        </div>
      )}

      {/* Cluster List */}
      <div className="mt-4 space-y-4">
        {!clusters || clusters.length === 0 ? (
          <div className="rounded-xl border border-dashed border-zinc-800/80 bg-zinc-900/20 p-8 text-center text-xs text-zinc-500 flex flex-col items-center gap-3">
            <Network className="w-8 h-8 text-zinc-700" />
            No active GPU clusters running. Launch a multi-node cluster to begin distributed inference or fine-tuning.
          </div>
        ) : (
          clusters.map((c) => (
            <motion.div
              key={c.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-xl border border-zinc-800/60 bg-zinc-900/40 backdrop-blur-md p-5 transition-all hover:border-zinc-700 hover:shadow-lg hover:shadow-zinc-900/50"
            >
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div className="space-y-2">
                  <div className="flex items-center gap-3">
                    <span className="font-semibold text-base text-zinc-100 flex items-center gap-2">
                      <Layers className="w-4 h-4 text-emerald-400" />
                      {c.name || c.id}
                    </span>
                    <span className={`rounded-full px-2.5 py-0.5 text-[10px] font-medium border flex items-center gap-1.5 ${
                      c.status === "active" ? "bg-emerald-950/50 text-emerald-300 border-emerald-800/60 shadow-[0_0_10px_rgba(16,185,129,0.1)]" :
                      c.status === "provisioning" ? "bg-amber-950/50 text-amber-300 border-amber-800/60" :
                      "bg-zinc-800/50 text-zinc-400 border-zinc-700"
                    }`}>
                      {c.status === "active" && <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />}
                      {c.status === "provisioning" && <RefreshCw className="w-3 h-3 animate-spin" />}
                      {c.status.toUpperCase()}
                    </span>
                  </div>
                  
                  <div className="flex flex-wrap items-center gap-4 text-xs text-zinc-400">
                    <div className="flex items-center gap-1.5 bg-zinc-950/50 px-2 py-1 rounded-md border border-zinc-800/50">
                      <Cpu className="w-3.5 h-3.5 text-zinc-500" />
                      <span>{c.node_count}x <strong className="text-zinc-200">{c.gpu_type}</strong></span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Server className="w-3.5 h-3.5 text-zinc-500" />
                      <span><strong className="text-zinc-200">{c.region}</strong></span>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Database className="w-3.5 h-3.5 text-zinc-500" />
                      <span><strong className="text-zinc-200">{c.filesystem}</strong></span>
                    </div>
                    {c.head_ip && (
                      <div className="flex items-center gap-1.5">
                        <span className="text-emerald-500/80 font-mono bg-emerald-950/30 px-1.5 py-0.5 rounded border border-emerald-900/30">
                          {c.head_ip}
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex flex-col items-end gap-2">
                  <div className="text-xs font-mono bg-zinc-950/80 px-2.5 py-1 rounded-md border border-zinc-800/80 text-zinc-300 flex items-center gap-2">
                    <span className="text-zinc-500">Burn Rate:</span>
                    <span className="text-amber-400 font-semibold">${getBurnRate(c).toFixed(2)}/hr</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {c.head_instance_id && c.status === "active" && (
                      <button
                        onClick={() => dockInstance(c.head_instance_id!, c.name || c.id)}
                        className="flex items-center gap-1.5 rounded border border-zinc-700/60 bg-zinc-800/50 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-700/80 hover:text-white transition-colors"
                        title="Open SSH Terminal to Head Node"
                      >
                        <Terminal className="w-3.5 h-3.5" />
                        SSH Head
                      </button>
                    )}
                    <button
                      onClick={() => setSelectedCluster(selectedCluster?.id === c.id ? null : c)}
                      className={`flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs transition-colors ${
                        selectedCluster?.id === c.id 
                          ? "border-emerald-700/50 bg-emerald-900/20 text-emerald-300 hover:bg-emerald-900/40" 
                          : "border-zinc-700/60 bg-zinc-800/50 text-zinc-300 hover:bg-zinc-700/80"
                      }`}
                    >
                      <Network className="w-3.5 h-3.5" />
                      {selectedCluster?.id === c.id ? "Hide Topology" : "Topology"}
                    </button>
                    {c.status !== "terminated" && (
                      <button
                        onClick={() => handleTerminate(c.id)}
                        disabled={termBusy === c.id}
                        className="flex items-center gap-1.5 rounded border border-red-900/50 bg-red-950/40 px-2.5 py-1 text-xs text-red-400 hover:bg-red-900/60 disabled:opacity-50 transition-colors"
                      >
                        <Power className="w-3.5 h-3.5" />
                        {termBusy === c.id ? "Stopping..." : "Terminate"}
                      </button>
                    )}
                  </div>
                </div>
              </div>

              {/* Node Details Expansion - Sleek Tree Topology */}
              <AnimatePresence>
                {selectedCluster?.id === c.id && c.nodes && (
                  <motion.div 
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    className="mt-5 border-t border-zinc-800/50 pt-4 overflow-hidden"
                  >
                    <h4 className="text-[11px] font-semibold text-zinc-500 uppercase tracking-widest mb-3">Cluster Topology Tree</h4>
                    <div className="relative pl-4 space-y-3">
                      {/* Vertical line connecting head to workers */}
                      <div className="absolute left-6 top-6 bottom-6 w-px bg-gradient-to-b from-purple-500/50 via-blue-500/20 to-transparent" />
                      
                      {c.nodes.sort((a, b) => a.role === "head" ? -1 : 1).map((node) => (
                        <div key={node.id} className="relative flex items-center gap-3">
                          {/* Horizontal connector branch */}
                          {node.role === "worker" && (
                            <div className="w-4 h-px bg-blue-500/30 ml-2" />
                          )}
                          <div className={`flex-1 flex items-center justify-between rounded-lg bg-zinc-950/60 p-3 border ${
                            node.role === "head" ? "border-purple-900/40 shadow-[0_0_15px_rgba(168,85,247,0.05)] ml-0" : "border-zinc-800/60"
                          }`}>
                            <div className="flex items-center gap-3">
                              <div className={`p-1.5 rounded-md ${node.role === "head" ? "bg-purple-900/30 text-purple-400" : "bg-blue-900/20 text-blue-400"}`}>
                                {node.role === "head" ? <Server className="w-4 h-4" /> : <Cpu className="w-4 h-4" />}
                              </div>
                              <div>
                                <div className="font-mono text-zinc-200 text-xs flex items-center gap-2">
                                  {node.role.toUpperCase()} #{node.node_index}
                                  <span className={`text-[9px] px-1.5 py-0.5 rounded-sm ${
                                    node.status === "active" ? "bg-emerald-950/50 text-emerald-400" : "bg-zinc-800 text-zinc-400"
                                  }`}>
                                    {node.status}
                                  </span>
                                </div>
                                <div className="text-zinc-500 text-[10px] mt-0.5">Instance: {node.instance_id}</div>
                              </div>
                            </div>
                            <div className="font-mono text-[11px] bg-zinc-900 px-2 py-1 rounded border border-zinc-800/80">
                              {node.ip ? (
                                <span className="text-zinc-300">{node.ip}</span>
                              ) : (
                                <span className="text-zinc-500 flex items-center gap-1.5">
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
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
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
              className="relative w-full max-w-lg rounded-2xl border border-zinc-700/50 bg-zinc-950/90 backdrop-blur-xl p-6 shadow-2xl shadow-black/80 overflow-hidden"
            >
              <div className="absolute top-0 left-0 w-full h-1 bg-gradient-to-r from-emerald-500 to-teal-400" />
              <h3 className="text-lg font-semibold text-zinc-100 mb-1">Launch Swarm</h3>
              <p className="text-xs text-zinc-400 mb-5">Provision a high-density multi-node GPU cluster.</p>

              {formErr && (
                <div className="mb-4 rounded-lg bg-red-950/40 border border-red-900/50 p-3 text-xs text-red-300">
                  {formErr}
                </div>
              )}

              <form onSubmit={handleLaunch} className="space-y-4 text-sm">
                <div>
                  <label className="block text-zinc-400 mb-1.5 text-xs font-medium">Cluster Name</label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2.5 text-zinc-200 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/50 transition-all placeholder-zinc-600"
                    placeholder="e.g. ray-cluster-01"
                    required
                  />
                </div>

                <div>
                  <label className="block text-zinc-400 mb-1.5 text-xs font-medium">Instance GPU Type</label>
                  <select
                    value={gpuType}
                    onChange={(e) => setGpuType(e.target.value)}
                    className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2.5 text-zinc-200 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/50 transition-all"
                  >
                    {Object.entries(types).map(([id, info]) => (
                      <option key={id} value={id}>
                        {info.description} (${info.price_usd_per_hour}/hr)
                      </option>
                    ))}
                    {!Object.keys(types).length && (
                      <>
                        <option value="gpu_8x_h100_sxm5">8x H100 SXM5 ($24.72/hr)</option>
                        <option value="gpu_8x_a100_80gb">8x A100 80GB ($15.12/hr)</option>
                        <option value="gpu_4x_a100_80gb">4x A100 80GB ($7.56/hr)</option>
                      </>
                    )}
                  </select>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-zinc-400 mb-1.5 text-xs font-medium">Node Count</label>
                    <input
                      type="number"
                      min={1}
                      max={128}
                      value={nodeCount}
                      onChange={(e) => setNodeCount(parseInt(e.target.value) || 1)}
                      className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2.5 text-zinc-200 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/50 transition-all"
                    />
                  </div>
                  <div>
                    <label className="block text-zinc-400 mb-1.5 text-xs font-medium">Region</label>
                    <select
                      value={region}
                      onChange={(e) => setRegion(e.target.value)}
                      className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2.5 text-zinc-200 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/50 transition-all"
                    >
                      {regions.map((r) => (
                        <option key={r.code} value={r.code}>{r.name}</option>
                      ))}
                      {!regions.length && <option value="us-east-1">US East 1</option>}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="block text-zinc-400 mb-1.5 text-xs font-medium">Persistent Storage Volume</label>
                  <select
                    value={filesystem}
                    onChange={(e) => setFilesystem(e.target.value)}
                    className="w-full rounded-lg border border-zinc-800 bg-zinc-900/50 px-3 py-2.5 text-zinc-200 focus:outline-none focus:border-emerald-500/50 focus:ring-1 focus:ring-emerald-500/50 transition-all"
                  >
                    {filesystems.map((fs) => (
                      <option key={fs.name} value={fs.name}>{fs.name} ({fs.region})</option>
                    ))}
                    {!filesystems.length && <option value="manifold-data">manifold-data</option>}
                  </select>
                </div>

                <div className="pt-4 flex justify-between items-center border-t border-zinc-800/50">
                  <div className="text-xs text-zinc-500 flex flex-col">
                    <span>Est. Burn Rate</span>
                    <span className="text-emerald-400 font-mono font-medium">
                      ${((types[gpuType]?.price_usd_per_hour || 24.72) * nodeCount).toFixed(2)}/hr
                    </span>
                  </div>
                  <div className="flex gap-3">
                    <button
                      type="button"
                      onClick={() => setShowLaunchModal(false)}
                      className="rounded-lg px-4 py-2 text-xs font-medium text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800/50 transition-colors"
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={launching}
                      className="rounded-lg bg-emerald-600/90 px-5 py-2 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50 transition-all shadow-[0_0_15px_rgba(16,185,129,0.2)]"
                    >
                      {launching ? "Provisioning..." : "Launch Swarm"}
                    </button>
                  </div>
                </div>
              </form>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}
