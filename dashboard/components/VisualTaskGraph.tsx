"use client";

import { useState, useMemo } from "react";
import { api, type Task } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  Node,
  Edge,
  NodeProps,
  useNodesState,
  useEdgesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

function TaskNode({ data }: { data: { task: Task } }) {
  const { task } = data;
  const isRunning = task.status === "running";
  const isFailed = task.status === "failed";
  const isSuccess = task.status === "succeeded";

  return (
    <div
      className={`rounded-lg border p-3.5 w-[220px] transition-all shadow-lg ${
        isRunning
          ? "border-cyan-500/80 bg-cyan-950/30 shadow-cyan-950/50"
          : isSuccess
          ? "border-emerald-800/80 bg-emerald-950/20"
          : isFailed
          ? "border-red-800/80 bg-red-950/20"
          : "border-zinc-800 bg-zinc-900"
      }`}
    >
      <Handle type="target" position={Position.Left} className="w-2 h-2 !bg-zinc-700 !border-0" />
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className="font-mono text-xs text-zinc-200 font-medium truncate">
          {task.template}
        </span>
        <span
          className={`h-2 w-2 rounded-full ${
            isRunning
              ? "bg-cyan-400 animate-ping"
              : isSuccess
              ? "bg-emerald-400"
              : isFailed
              ? "bg-red-400"
              : "bg-zinc-600"
          }`}
        />
      </div>

      <div className="text-[11px] text-zinc-400 font-mono truncate">
        ID: {task.id.slice(0, 8)}
      </div>

      <div className="mt-2 flex items-center justify-between text-[10px] text-zinc-500 border-t border-zinc-800/80 pt-1.5">
        <span>{task.instance_id ? task.instance_id.slice(0, 10) : "local"}</span>
        <span className="uppercase font-semibold tracking-wider text-zinc-400">
          {task.status}
        </span>
      </div>
      <Handle type="source" position={Position.Right} className="w-2 h-2 !bg-zinc-700 !border-0" />
    </div>
  );
}

const nodeTypes = {
  taskNode: TaskNode,
};

export function VisualTaskGraph() {
  const { data: history } = usePolling(api.tasks, 4000);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  const tasks = history || [];

  const initialNodes: Node[] = useMemo(() => {
    return tasks.map((task, idx) => ({
      id: task.id,
      position: { x: idx * 280, y: 100 },
      data: { task },
      type: "taskNode",
    }));
  }, [tasks]);

  const initialEdges: Edge[] = useMemo(() => {
    return tasks.slice(1).map((task, idx) => {
      const prevTask = tasks[idx];
      const isRunning = task.status === "running";
      return {
        id: `e-${prevTask.id}-${task.id}`,
        source: prevTask.id,
        target: task.id,
        animated: isRunning,
        style: { stroke: isRunning ? "#06b6d4" : "#52525b", strokeWidth: 2 },
      };
    });
  }, [tasks]);

  const onNodeClick = (_: React.MouseEvent, node: Node) => {
    if (node.data && node.data.task) {
      setSelectedTask(node.data.task as Task);
    }
  };

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-950 p-6 shadow-xl text-zinc-100 flex flex-col">
      <div className="flex items-center justify-between border-b border-zinc-800 pb-4 shrink-0">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-100 flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-cyan-500 animate-pulse" />
            Visual Agent Task Graph
          </h2>
          <p className="text-xs text-zinc-400 mt-0.5">
            Real-time execution workflow, subagent task trees, and cluster jobs
          </p>
        </div>
        <span className="rounded-full bg-zinc-900 border border-zinc-800 px-3 py-1 text-xs text-zinc-400 font-mono">
          {tasks.filter((t) => t.status === "running").length} Active Node(s)
        </span>
      </div>

      {/* Visual Canvas */}
      <div className="mt-4 h-[400px] w-full rounded-lg border border-zinc-800 bg-zinc-900/40 relative overflow-hidden shrink-0">
        {tasks.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
            No active or historical tasks in workflow graph.
          </div>
        ) : (
          <ReactFlow
            nodes={initialNodes}
            edges={initialEdges}
            nodeTypes={nodeTypes}
            onNodeClick={onNodeClick}
            fitView
            proOptions={{ hideAttribution: true }}
            className="dark"
          >
            <Background color="#27272a" gap={16} />
            <Controls className="!bg-zinc-900 !border-zinc-800 !fill-zinc-400" />
            <MiniMap 
              nodeStrokeColor="#3f3f46" 
              nodeColor="#18181b" 
              maskColor="rgba(0, 0, 0, 0.2)"
              className="!bg-zinc-950 !border !border-zinc-800"
            />
          </ReactFlow>
        )}
      </div>

      {/* Task Output Drawer */}
      {selectedTask && (
        <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-900 p-4 shrink-0">
          <div className="flex items-center justify-between border-b border-zinc-800 pb-2 mb-3">
            <h3 className="text-xs font-semibold text-zinc-200 font-mono">
              Task Node Output: {selectedTask.template} ({selectedTask.id})
            </h3>
            <button
              onClick={() => setSelectedTask(null)}
              className="text-xs text-zinc-500 hover:text-zinc-300"
            >
              Close
            </button>
          </div>
          <div className="grid grid-cols-2 gap-4 text-xs">
            <div>
              <span className="text-zinc-500 block mb-1">Parameters:</span>
              <pre className="rounded bg-zinc-950 p-2 text-zinc-300 font-mono text-[11px] overflow-x-auto max-h-32">
                {JSON.stringify(selectedTask.parameters, null, 2)}
              </pre>
            </div>
            <div>
              <span className="text-zinc-500 block mb-1">Status & Execution:</span>
              <div className="rounded bg-zinc-950 p-2 text-zinc-300 font-mono text-[11px] space-y-1">
                <div>
                  Status: <span className="text-emerald-400">{selectedTask.status}</span>
                </div>
                <div>Exit Code: {selectedTask.exit_code ?? "N/A"}</div>
                <div>
                  Started:{" "}
                  {selectedTask.started_at
                    ? new Date(selectedTask.started_at).toLocaleTimeString()
                    : "N/A"}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
