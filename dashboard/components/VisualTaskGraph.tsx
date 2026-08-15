"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Task } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  MarkerType,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

const NODE_W = 220;
const COL_GAP = 60;
const ROW_GAP = 30;
const NODE_H = 104;          // measured card height; only layout math uses it
const COL_STRIDE = NODE_W + COL_GAP;
const ROW_STRIDE = NODE_H + ROW_GAP;

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
      {/* Handles only where an edge can actually land. An isolated job has
          no run-after relationship, and dangling connector dots on 41 of
          them read as "these connect somehow" - the exact false impression
          this panel was rebuilt to stop making. */}
      {data.task.depends_on && data.task.depends_on.length > 0 && (
        <Handle
          type="target"
          position={Position.Left}
          className="w-2 h-2 !bg-zinc-400 !border-0"
        />
      )}
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
      <Handle
        type="source"
        position={Position.Right}
        className="w-2 h-2 !bg-zinc-400 !border-0"
      />
    </div>
  );
}

const nodeTypes = { taskNode: TaskNode };

// Layout and edges both come from depends_on, the REAL dependency data. (An
// earlier version drew an arrow from each task to the next one in list
// order, presenting two unrelated jobs as a pipeline. Edges that do not
// exist in the scheduler must not exist on screen.)
//
// CHAINS get strict columns: x = depth, so "further right" always means
// "runs later". ISOLATED jobs - no parents and no children - are gridded
// instead of stacked, because for them x carries NO dependency meaning and
// a single column does not scale: the live backend had 41 independent jobs,
// which at one per row is a 6,150px column inside a 400px canvas. Nothing
// could fit that, which is exactly how this panel came to show a clipped
// sliver of an unreadable list.
export function layoutTasks(tasks: Task[]): { nodes: Node[]; edges: Edge[] } {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const parentsOf = (t: Task) =>
    (t.depends_on ?? []).filter((p) => byId.has(p));

  const hasChild = new Set<string>();
  for (const t of tasks) for (const p of parentsOf(t)) hasChild.add(p);

  const connected = tasks.filter(
    (t) => parentsOf(t).length > 0 || hasChild.has(t.id),
  );
  const isolated = tasks.filter((t) => !connected.includes(t));

  // Depth = longest dependency chain above the task.
  const depths = new Map<string, number>();
  const depth = (id: string, seen: Set<string> = new Set()): number => {
    const known = depths.get(id);
    if (known !== undefined) return known;
    if (seen.has(id)) return 0; // impossible by construction; stay safe
    seen.add(id);
    const task = byId.get(id);
    const parents = task ? parentsOf(task) : [];
    const d = parents.length === 0
      ? 0
      : 1 + Math.max(...parents.map((p) => depth(p, seen)));
    depths.set(id, d);
    return d;
  };

  const rows = new Map<number, number>(); // depth -> next free row
  const nodes: Node[] = connected.map((task) => {
    const d = depth(task.id);
    const row = rows.get(d) ?? 0;
    rows.set(d, row + 1);
    return {
      id: task.id,
      position: { x: d * COL_STRIDE, y: row * ROW_STRIDE },
      data: { task },
      type: "taskNode",
    };
  });

  // The chain block's height, so the independent grid starts clear of it.
  const chainRows = Math.max(0, ...Array.from(rows.values()));
  const gridTop = chainRows > 0 ? chainRows * ROW_STRIDE + ROW_STRIDE : 0;

  // Roughly square, so fitView lands on a readable zoom either way.
  const cols = Math.min(6, Math.max(1, Math.ceil(Math.sqrt(isolated.length))));
  isolated.forEach((task, i) => {
    nodes.push({
      id: task.id,
      position: {
        x: (i % cols) * COL_STRIDE,
        y: gridTop + Math.floor(i / cols) * ROW_STRIDE,
      },
      data: { task },
      type: "taskNode",
    });
  });

  const edges: Edge[] = tasks.flatMap((task) =>
    parentsOf(task).map((parentId) => {
      const isRunning = task.status === "running";
      const isSkipped = task.status === "skipped";
      const stroke = isRunning ? "#f59e0b" : isSkipped ? "#a1a1aa" : "#71717a";
      return {
        id: `e-${parentId}-${task.id}`,
        source: parentId,
        target: task.id,
        animated: isRunning,
        // The panel's own words are "an arrow means runs after" - so draw an
        // arrow. These edges carried no marker at all, which made the one
        // sentence explaining the whole graph untrue.
        markerEnd: { type: MarkerType.ArrowClosed, color: stroke,
                     width: 18, height: 18 },
        style: {
          stroke,
          strokeWidth: 2,
          // A dead edge (the child was skipped) reads as severed.
          ...(isSkipped ? { strokeDasharray: "6 4" } : {}),
        },
      };
    }),
  );
  return { nodes, edges };
}

function Canvas({
  tasks,
  onSelect,
}: {
  tasks: Task[];
  onSelect: (t: Task) => void;
}) {
  const computed = useMemo(() => layoutTasks(tasks), [tasks]);
  const [nodes, setNodes, onNodesChange] = useNodesState(computed.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(computed.edges);
  const { fitView } = useReactFlow();

  // Which jobs are on screen, not how often we polled: the list is rebuilt
  // every 4s with fresh object identities, and re-fitting on identity alone
  // would yank the viewport out from under anyone reading it.
  const signature = computed.nodes.map((n) => n.id).join(",") +
    "|" + tasks.map((t) => t.status).join("");

  useEffect(() => {
    setNodes(computed.nodes);
    setEdges(computed.edges);
    // Re-fit AFTER React Flow has measured the new nodes. `fitView` as a
    // prop only fires once at mount, so a graph that grows by polling
    // silently drifts off screen - which is what left this panel showing a
    // clipped middle band of a much taller column.
    const frame = requestAnimationFrame(() =>
      fitView({ padding: 0.12, duration: 250 }),
    );
    return () => cancelAnimationFrame(frame);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);

  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      const task = (node.data as { task?: Task })?.task;
      if (task) onSelect(task);
    },
    [onSelect],
  );

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      nodeTypes={nodeTypes}
      onNodeClick={onNodeClick}
      fitView
      fitViewOptions={{ padding: 0.12 }}
      // The default floor is 0.5, which cannot fit a tall graph into a
      // short canvas - fitView silently gives up and the graph stays
      // clipped. 0.04 lets it zoom out to whatever the data needs.
      minZoom={0.04}
      maxZoom={1.5}
      proOptions={{ hideAttribution: true }}
      colorMode="dark"
    >
      <Background gap={16} />
      <Controls className="!bg-zinc-100 !border-zinc-200 !fill-zinc-500" />
      {/* Only once the graph is big enough to need one. At 200x150 on a
          400px canvas the minimap covered a quarter of the view, and its
          zinc-950 background on a zinc-950 canvas rendered as an opaque
          black slab sitting over the jobs. */}
      {nodes.length > 12 && (
        <MiniMap
          pannable
          zoomable
          // Literal hex, not a zinc class: the dark theme remaps the zinc
          // scale, so `bg-zinc-900` here rendered as a LIGHT panel against
          // the dark canvas - the same trap the template editor documents.
          style={{ width: 132, height: 92, backgroundColor: "#18181b",
                   border: "1px solid #3f3f46", borderRadius: 6 }}
          maskColor="rgba(9, 9, 11, 0.6)"
          nodeStrokeWidth={0}
          nodeColor={(n) => {
            const s = (n.data as { task?: Task })?.task?.status;
            return s === "succeeded"
              ? "#34d399"
              : s === "failed"
              ? "#f87171"
              : s === "running"
              ? "#fbbf24"
              : "#71717a";
          }}
        />
      )}
    </ReactFlow>
  );
}

export function VisualTaskGraph() {
  const { data: history } = usePolling(api.tasks, 4000);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const tasks = useMemo(() => history || [], [history]);

  const chained = tasks.filter((t) => (t.depends_on ?? []).length > 0).length;

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
            means &ldquo;runs after&rdquo;; independent jobs are gridded
            together and carry no arrows.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {/* Says what the picture is BEFORE you read it: mostly-independent
              jobs are the normal case, and a graph with no arrows is not a
              broken graph. */}
          <span className="rounded-full bg-zinc-100 border border-zinc-200 px-3 py-1 text-xs text-zinc-600 font-mono">
            {tasks.length} job{tasks.length === 1 ? "" : "s"}
            {chained > 0 ? ` - ${chained} chained` : " - none chained"}
          </span>
          <span className="rounded-full bg-zinc-100 border border-zinc-200 px-3 py-1 text-xs text-zinc-600 font-mono">
            {tasks.filter((t) => t.status === "running").length} running
          </span>
        </div>
      </div>

      {/* Visual Canvas */}
      <div className="mt-4 h-[460px] w-full rounded-lg border border-zinc-200 bg-zinc-950 relative overflow-hidden shrink-0">
        {tasks.length === 0 ? (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-zinc-500">
            No jobs yet. Queue one on the Jobs page; chain jobs with &ldquo;Run
            after&rdquo; and the arrows appear here.
          </div>
        ) : (
          <ReactFlowProvider>
            <Canvas tasks={tasks} onSelect={setSelectedTask} />
          </ReactFlowProvider>
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
              <span className="text-zinc-500 block mb-1">Status &amp; Execution:</span>
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
