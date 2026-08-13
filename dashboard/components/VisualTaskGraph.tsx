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
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

function TaskNode({ data }: { data: { task: Task } }) {
  const { task } = data;
  const isRunning = task.status === "running";
  const isFailed = task.status === "failed";
  const isSuccess = task.status === "succeeded";
  const isSkipped = task.status === "skipped";

  return (
    <div
      className={`rounded-lg border p-3.5 w-[220px] transition-all shadow-sm ${
        isRunning
          ? "border-amber-200 bg-amber-50"
          : isSuccess
          ? "border-emerald-200 bg-emerald-50"
          : isFailed
          ? "border-red-200 bg-red-50"
          : isSkipped
          ? "border-zinc-200 bg-zinc-100 opacity-70"
          : "border-zinc-200 bg-white"
      }`}
    >
      <Handle type="target" position={Position.Left} className="w-2 h-2 !bg-zinc-400 !border-0" />
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <span className="font-mono text-xs text-zinc-900 font-medium truncate">
          {task.template}
        </span>
        <span
          className={`h-2 w-2 rounded-full ${
            isRunning
              ? "bg-amber-400 animate-ping"
              : isSuccess
              ? "bg-emerald-400"
              : isFailed
              ? "bg-red-400"
              : "bg-zinc-400"
          }`}
        />
      </div>

      <div className="text-[11px] text-zinc-500 font-mono truncate">
        ID: {task.id.slice(0, 8)}
      </div>

      <div className="mt-2 flex items-center justify-between text-[10px] text-zinc-500 border-t border-zinc-200 pt-1.5">
        <span>{task.instance_id ? task.instance_id.slice(0, 10) : "local"}</span>
        <span className="uppercase font-semibold tracking-wider text-zinc-600">
          {task.status}
        </span>
      </div>
      <Handle type="source" position={Position.Right} className="w-2 h-2 !bg-zinc-400 !border-0" />
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

  // Layout and edges both come from depends_on, the REAL dependency data.
  // (An earlier version drew an arrow from each task to the next one in
  // list order, which presented two unrelated jobs as a pipeline. Edges
  // that do not exist in the scheduler must not exist on screen.)
  //
  // Depth = longest dependency chain above the task: parents at depth d,
  // children at d+1, independent jobs all in column 0.
  const { initialNodes, initialEdges } = useMemo(() => {
    const byId = new Map(tasks.map((t) => [t.id, t]));
    const depths = new Map<string, number>();
    const depth = (id: string, seen: Set<string> = new Set()): number => {
      const known = depths.get(id);
      if (known !== undefined) return known;
      if (seen.has(id)) return 0; // impossible by construction; stay safe
      seen.add(id);
      const task = byId.get(id);
      const parents = (task?.depends_on ?? []).filter((p) => byId.has(p));
      const d =
        parents.length === 0
          ? 0
          : 1 + Math.max(...parents.map((p) => depth(p, seen)));
      depths.set(id, d);
      return d;
    };

    const rows = new Map<number, number>(); // depth -> next free row
    const nodes: Node[] = tasks.map((task) => {
      const d = depth(task.id);
      const row = rows.get(d) ?? 0;
      rows.set(d, row + 1);
      return {
        id: task.id,
        position: { x: d * 280, y: row * 150 },
        data: { task },
        type: "taskNode",
      };
    });

    const edges: Edge[] = tasks.flatMap((task) =>
      (task.depends_on ?? [])
        .filter((p) => byId.has(p)) // a deleted parent has no node to point from
        .map((parentId) => {
          const isRunning = task.status === "running";
          const isSkipped = task.status === "skipped";
          return {
            id: `e-${parentId}-${task.id}`,
            source: parentId,
            target: task.id,
            animated: isRunning,
            style: {
              stroke: isRunning
                ? "var(--color-amber-500)"
                : "var(--color-zinc-300)",
              strokeWidth: 2,
              // A dead edge (the child was skipped) reads as severed.
              ...(isSkipped ? { strokeDasharray: "6 4" } : {}),
            },
          };
        }),
    );
    return { initialNodes: nodes, initialEdges: edges };
  }, [tasks]);

  const onNodeClick = (_: React.MouseEvent, node: Node) => {
    if (node.data && node.data.task) {
      setSelectedTask(node.data.task as Task);
    }
  };

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm flex flex-col">
      <div className="flex items-center justify-between border-b border-zinc-200 pb-4 shrink-0">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-zinc-900 flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-sky-500 animate-pulse" />
            Job pipeline
          </h2>
          <p className="text-xs text-zinc-500 mt-0.5">
            Every job, with its declared run-after dependencies. An arrow
            means "runs after"; jobs with no arrows are independent.
          </p>
        </div>
        <span className="rounded-full bg-zinc-100 border border-zinc-200 px-3 py-1 text-xs text-zinc-600 font-mono">
          {tasks.filter((t) => t.status === "running").length} running
        </span>
      </div>

      {/* Visual Canvas */}
      <div className="mt-4 h-[400px] w-full rounded-lg border border-zinc-200 bg-zinc-950 relative overflow-hidden shrink-0">
        {tasks.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
            No jobs yet. Queue one on the Jobs page; chain jobs with "Run
            after" and the arrows appear here.
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
            <Background color="var(--color-zinc-200)" gap={16} />
            <Controls className="!bg-zinc-100 !border-zinc-200 !fill-zinc-500" />
            <MiniMap
              nodeStrokeColor="var(--color-zinc-400)"
              nodeColor="var(--color-zinc-300)"
              maskColor="rgba(0, 0, 0, 0.4)"
              className="!bg-zinc-950 !border !border-zinc-200"
            />
          </ReactFlow>
        )}
      </div>

      {/* Task Output Drawer */}
      {selectedTask && (
        <div className="mt-4 rounded-lg border border-zinc-200 bg-zinc-50 p-4 shrink-0">
          <div className="flex items-center justify-between border-b border-zinc-200 pb-2 mb-3">
            <h3 className="text-xs font-semibold text-zinc-900 font-mono">
              Task Node Output: {selectedTask.template} ({selectedTask.id})
            </h3>
            <button
              onClick={() => setSelectedTask(null)}
              className="text-xs text-zinc-500 hover:text-zinc-900"
            >
              Close
            </button>
          </div>
          <div className="grid grid-cols-2 gap-4 text-xs">
            <div>
              <span className="text-zinc-500 block mb-1">Parameters:</span>
              <pre className="rounded bg-zinc-950 p-2 text-zinc-600 font-mono text-[11px] overflow-x-auto max-h-32">
                {JSON.stringify(selectedTask.parameters, null, 2)}
              </pre>
            </div>
            <div>
              <span className="text-zinc-500 block mb-1">Status & Execution:</span>
              <div className="rounded bg-zinc-950 p-2 text-zinc-600 font-mono text-[11px] space-y-1">
                <div>
                  Status: <span className="text-emerald-700">{selectedTask.status}</span>
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
