import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Brain, Pause, Play, Sparkles, Target, Trash2, TrendingUp } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { Switch } from "@/components/ui/switch";
import { fetchJSON } from "@/lib/api";

interface SkillStat {
  name: string;
  uses: number;
  successes: number;
  success_rate: number | null;
}
interface LearningMetrics {
  sessions: number;
  forward_transfer: number | null;
  backward_transfer: number | null;
  forgetting: number | null;
  skill_diversity: number | null;
  diversity_trend: number | null;
  warnings: string[];
  summary: string;
}
interface LearningCurve {
  points: { ts: string; pass_rate: number | null }[];
  learned: string[];
  regressed: string[];
  draft_pins: number;
}
interface LearningStats {
  overall: { sessions: number; successes: number; success_rate: number | null };
  recent_success_rate: number | null;
  skills: SkillStat[];
  personas: Record<string, { uses: number; successes: number; success_rate: number | null }>;
  metrics?: LearningMetrics;
  lessons?: { total: number; by_task_type: Record<string, number> };
  curve?: LearningCurve;
  playbook?: { enabled: boolean; total: number; by_scope: Record<string, number> };
  consensus?: { enabled: boolean; tiers: Record<string, string>; ensemble: boolean };
}
interface Aspiration {
  id: string;
  title: string;
  status: string;
  roadmap?: string[];
}
interface Interest {
  id: string;
  field: string;
  status: string;
}
interface GraphNode {
  name: string;
  promotion_level: number;
  verdict: string;
  success_rate: number | null;
  uses: number | null;
  dependencies: string[];
}
interface SleepStatus {
  paused: boolean;
  last_run: string | null;
  last_report: Record<string, unknown> | null;
}

const pct = (x: number | null | undefined): string =>
  typeof x === "number" ? `${Math.round(x * 100)}%` : "—";

// signed delta (e.g. forward/backward transfer): +12% / −8% / —
const signed = (x: number | null | undefined): string =>
  typeof x === "number" ? `${x >= 0 ? "+" : "−"}${Math.round(Math.abs(x) * 100)}%` : "—";

const deltaColor = (x: number | null | undefined): string =>
  typeof x !== "number" ? "text-muted-foreground" : x > 0.02 ? "text-success" : x < -0.02 ? "text-destructive" : "text-muted-foreground";

const VERDICT_COLOR: Record<string, string> = {
  promotable: "text-success",
  refine: "text-warning",
  stable: "text-muted-foreground",
};

export default function LearningPage() {
  const [stats, setStats] = useState<LearningStats | null>(null);
  const [aspirations, setAspirations] = useState<Aspiration[]>([]);
  const [interests, setInterests] = useState<Interest[]>([]);
  const [graph, setGraph] = useState<GraphNode[]>([]);
  const [sleep, setSleep] = useState<SleepStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [newGoal, setNewGoal] = useState("");
  const [newField, setNewField] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [s, a, i, g, sl] = await Promise.all([
        fetchJSON<LearningStats>("/api/learning/stats"),
        fetchJSON<{ aspirations: Aspiration[] }>("/api/aspirations"),
        fetchJSON<{ interests: Interest[] }>("/api/interests"),
        fetchJSON<{ nodes: GraphNode[] }>("/api/skills/graph"),
        fetchJSON<SleepStatus>("/api/sleep"),
      ]);
      setStats(s);
      setAspirations(a.aspirations);
      setInterests(i.interests);
      setGraph(g.nodes);
      setSleep(sl);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const addAspiration = async () => {
    const goal = newGoal.trim();
    if (!goal) return;
    setNewGoal("");
    await fetchJSON("/api/aspirations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    void refresh();
  };
  const removeAspiration = async (id: string) => {
    await fetchJSON(`/api/aspirations/${encodeURIComponent(id)}`, { method: "DELETE" });
    void refresh();
  };
  const addInterest = async () => {
    const field = newField.trim();
    if (!field) return;
    setNewField("");
    await fetchJSON("/api/interests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field }),
    });
    void refresh();
  };
  const removeInterest = async (id: string) => {
    await fetchJSON(`/api/interests/${encodeURIComponent(id)}`, { method: "DELETE" });
    void refresh();
  };
  const toggleSleep = async () => {
    if (!sleep) return;
    await fetchJSON("/api/sleep/paused", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: !sleep.paused }),
    });
    void refresh();
  };
  const toggleFlag = async (path: string, enabled: boolean) => {
    await fetchJSON(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    void refresh();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center p-12">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="space-y-6 p-4">
      <h2 className="text-xl font-semibold tracking-tight flex items-center gap-2">
        <Brain className="h-5 w-5" /> Self-Learning
      </h2>

      {/* Outcome reinforcement metrics */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="text-sm font-medium text-muted-foreground">Outcome reinforcement</div>
          <div className="flex gap-6 text-sm">
            <div>Sessions: <strong>{stats?.overall.sessions ?? 0}</strong></div>
            <div>Success: <strong>{pct(stats?.overall.success_rate)}</strong></div>
            <div>Recent (20): <strong>{pct(stats?.recent_success_rate)}</strong></div>
          </div>
          {stats && stats.skills.length > 0 && (
            <div className="space-y-1">
              <div className="text-xs text-muted-foreground">Skills by success rate</div>
              {stats.skills.slice(0, 10).map((s) => (
                <div key={s.name} className="flex justify-between text-sm">
                  <span>{s.name}</span>
                  <span className="text-muted-foreground">
                    {pct(s.success_rate)} ({s.successes}/{s.uses})
                  </span>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Continual-learning health */}
      {stats?.metrics && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              <TrendingUp className="h-4 w-4" /> Continual-learning health
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
              <div>
                Forward transfer:{" "}
                <strong className={deltaColor(stats.metrics.forward_transfer)}>
                  {signed(stats.metrics.forward_transfer)}
                </strong>
              </div>
              <div>
                Backward transfer:{" "}
                <strong className={deltaColor(stats.metrics.backward_transfer)}>
                  {signed(stats.metrics.backward_transfer)}
                </strong>
              </div>
              <div>
                Forgetting:{" "}
                <strong className={deltaColor(stats.metrics.forgetting != null ? -stats.metrics.forgetting : null)}>
                  {pct(stats.metrics.forgetting)}
                </strong>
              </div>
              <div>
                Skill diversity:{" "}
                <strong>{pct(stats.metrics.skill_diversity)}</strong>
                <span className={`ml-1 text-xs ${deltaColor(stats.metrics.diversity_trend)}`}>
                  ({signed(stats.metrics.diversity_trend)})
                </span>
              </div>
            </div>
            <div className="text-xs text-muted-foreground">{stats.metrics.summary}</div>
            {stats.metrics.warnings.map((w) => (
              <div key={w} className="flex items-center gap-1.5 text-xs text-warning">
                <AlertTriangle className="h-3.5 w-3.5" /> {w}
              </div>
            ))}
            {stats.lessons != null && stats.lessons.total > 0 && (
              <div className="text-xs text-muted-foreground">
                {stats.lessons.total} lesson{stats.lessons.total === 1 ? "" : "s"} learned from past
                failures (recalled via the <code>recall_lessons</code> tool).
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Learning curve */}
      {stats?.curve && stats.curve.points.length > 0 && (
        <Card>
          <CardContent className="space-y-2 p-4">
            <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              <TrendingUp className="h-4 w-4" /> Learning curve (eval pass-rate)
            </div>
            <div className="text-sm">
              latest:{" "}
              <strong>{pct(stats.curve.points[stats.curve.points.length - 1].pass_rate)}</strong>
              <span className="ml-2 text-muted-foreground">
                over {stats.curve.points.length} runs
              </span>
            </div>
            {stats.curve.learned.length > 0 && (
              <div className="text-xs text-success">learned: {stats.curve.learned.join(", ")}</div>
            )}
            {stats.curve.regressed.length > 0 && (
              <div className="text-xs text-destructive">regressed: {stats.curve.regressed.join(", ")}</div>
            )}
            {stats.curve.draft_pins > 0 && (
              <div className="text-xs text-muted-foreground">
                {stats.curve.draft_pins} regression-pin draft(s) awaiting review in evals/.drafts/
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Consensus routing */}
      {stats?.consensus && (
        <Card>
          <CardContent className="space-y-2 p-4">
            <div className="flex items-center justify-between text-sm font-medium">
              <span>Consensus routing</span>
              <Switch
                checked={stats.consensus.enabled}
                onCheckedChange={(v) => void toggleFlag("/api/learning/consensus/enabled", v)}
                aria-label="Toggle consensus routing"
              />
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs text-muted-foreground">
              {(["cheap", "mid", "smart"] as const).map((t) => (
                <div key={t}>
                  {t}: <span className="text-foreground">{stats.consensus!.tiers[t] || "inherit"}</span>
                </div>
              ))}
            </div>
            <div className="text-xs text-muted-foreground">
              ensemble on hardest tasks: {stats.consensus.ensemble ? "on" : "off"}
            </div>
            {!stats.consensus.enabled && (
              <div className="text-xs text-muted-foreground">
                Enable in <code>janus setup</code> or <code>janus config set consensus.enabled true</code>.
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ACE playbook */}
      {stats?.playbook && (
        <Card>
          <CardContent className="space-y-2 p-4">
            <div className="flex items-center justify-between text-sm font-medium">
              <span>ACE playbook (loop self-tuning)</span>
              <Switch
                checked={stats.playbook.enabled}
                onCheckedChange={(v) => void toggleFlag("/api/learning/playbook/enabled", v)}
                aria-label="Toggle ACE playbook"
              />
            </div>
            <div className="text-sm">
              <strong>{stats.playbook.total}</strong> learned guidance entr
              {stats.playbook.total === 1 ? "y" : "ies"} the loop applies to its own prompts
            </div>
            {Object.keys(stats.playbook.by_scope).length > 0 && (
              <div className="flex flex-wrap gap-1">
                {Object.entries(stats.playbook.by_scope).map(([scope, n]) => (
                  <Badge key={scope}>{scope}: {n}</Badge>
                ))}
              </div>
            )}
            <div className="text-xs text-muted-foreground">
              Inspect with <code>janus learning playbook</code>. Red-team-gated, capped.
            </div>
          </CardContent>
        </Card>
      )}

      {/* Aspirations */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Target className="h-4 w-4" /> Aspirations
          </div>
          <div className="flex gap-2">
            <Input
              placeholder="A long-term goal Janus should hold…"
              value={newGoal}
              onChange={(e) => setNewGoal(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addAspiration()}
            />
            <Button onClick={addAspiration}>Add</Button>
          </div>
          {aspirations.map((a) => (
            <div key={a.id} className="flex items-center justify-between text-sm">
              <span>
                {a.title} <Badge>{a.status}</Badge>
              </span>
              <Button variant="ghost" size="icon" onClick={() => removeAspiration(a.id)}>
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Interests */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" /> Interests (proactively researched)
          </div>
          <div className="flex gap-2">
            <Input
              placeholder="A field to keep up with (e.g. graphic design)…"
              value={newField}
              onChange={(e) => setNewField(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addInterest()}
            />
            <Button onClick={addInterest}>Add</Button>
          </div>
          {interests.map((i) => (
            <div key={i.id} className="flex items-center justify-between text-sm">
              <span>{i.field}</span>
              <Button variant="ghost" size="icon" onClick={() => removeInterest(i.id)}>
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Skill graph */}
      <Card>
        <CardContent className="space-y-2 p-4">
          <div className="text-sm font-medium">Skill graph (verifiable-reward promotion)</div>
          {graph.length === 0 ? (
            <div className="text-sm text-muted-foreground">No agent-created skills yet.</div>
          ) : (
            graph.map((n) => (
              <div key={n.name} className="flex justify-between text-sm">
                <span>
                  L{n.promotion_level} · {n.name}
                  {n.dependencies.length > 0 && (
                    <span className="text-muted-foreground"> ← {n.dependencies.join(", ")}</span>
                  )}
                </span>
                <span className={VERDICT_COLOR[n.verdict] ?? ""}>
                  {n.verdict} · {pct(n.success_rate)}
                </span>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      {/* Sleep */}
      <Card>
        <CardContent className="flex items-center justify-between p-4">
          <div className="text-sm">
            <span className="font-medium">Sleep consolidation</span>
            <span className="ml-2 text-muted-foreground">
              {sleep?.paused ? "paused" : "active"} · last run: {sleep?.last_run ?? "never"}
            </span>
          </div>
          <Button variant="outline" size="sm" onClick={toggleSleep}>
            {sleep?.paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
            <span className="ml-1">{sleep?.paused ? "Resume" : "Pause"}</span>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
