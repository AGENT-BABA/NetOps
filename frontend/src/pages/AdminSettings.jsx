import { useEffect, useState } from "react";
import { api, apiErrorMessage } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Wifi, WifiOff, Save, TestTube2, CheckCircle2, XCircle } from "lucide-react";
import { toast } from "sonner";

export default function AdminSettings() {
  const [config, setConfig] = useState({ host: "", port: "8728", username: "", password: "", use_ssl: false });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const { data } = await api.get("/admin/mikrotik/config");
        if (data.configured) {
          setConfig({ host: data.host || "", port: String(data.port || "8728"), username: data.username || "", password: "", use_ssl: data.use_ssl === true });
        }
      } catch (e) { toast.error(apiErrorMessage(e)); }
      finally { setLoading(false); }
    })();
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await api.post("/admin/mikrotik/config", {
        host: config.host,
        port: Number(config.port),
        username: config.username,
        password: config.password,
        use_ssl: config.use_ssl,
      });
      toast.success("MikroTik config saved");
    } catch (e) { toast.error(apiErrorMessage(e)); }
    finally { setSaving(false); }
  };

  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const { data } = await api.get("/admin/mikrotik/test");
      setTestResult(data);
    } catch (e) { toast.error(apiErrorMessage(e)); }
    finally { setTesting(false); }
  };

  return (
    <div className="space-y-6">
      <div>
        <div className="font-label text-[10px] text-muted-foreground">SYSTEM CONFIGURATION</div>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight sm:text-4xl">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Configure MikroTik RouterOS API connection for live PPPoE session monitoring.
        </p>
      </div>

      {/* MikroTik Config */}
      <Card className="border-border max-w-2xl">
        <CardHeader className="border-b border-border">
          <CardTitle className="font-display text-lg flex items-center gap-2">
            <Wifi className="h-5 w-5 text-cyan-500" />
            MikroTik CCR Connection
          </CardTitle>
        </CardHeader>
        <CardContent className="p-6 space-y-4">
          {loading ? (
            <div className="space-y-3"><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /></div>
          ) : (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label className="font-label text-[10px]">ROUTER IP</Label>
                  <Input
                    value={config.host}
                    onChange={(e) => setConfig({ ...config, host: e.target.value })}
                    placeholder="192.168.88.1"
                    className="font-mono"
                    data-testid="mikrotik-host"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="font-label text-[10px]">API PORT</Label>
                  <Input
                    value={config.port}
                    onChange={(e) => setConfig({ ...config, port: e.target.value })}
                    placeholder="8729"
                    className="font-mono"
                    data-testid="mikrotik-port"
                  />
                </div>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label className="font-label text-[10px]">API USERNAME</Label>
                  <Input
                    value={config.username}
                    onChange={(e) => setConfig({ ...config, username: e.target.value })}
                    placeholder="netops-api (dedicated user, not admin)"
                    className="font-mono"
                    data-testid="mikrotik-username"
                  />
                </div>
                <div className="space-y-2">
                  <Label className="font-label text-[10px]">API PASSWORD</Label>
                  <Input
                    type="password"
                    value={config.password}
                    onChange={(e) => setConfig({ ...config, password: e.target.value })}
                    placeholder="••••••••"
                    className="font-mono"
                    data-testid="mikrotik-password"
                  />
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Switch
                  checked={config.use_ssl}
                  onCheckedChange={(v) => setConfig({ ...config, use_ssl: v })}
                  data-testid="mikrotik-ssl"
                />
                <Label className="font-label text-[10px]">USE API-SSL (recommended)</Label>
              </div>
              <div className="flex gap-2 pt-2">
                <Button onClick={save} disabled={saving || !config.host || !config.username} className="gap-2" data-testid="mikrotik-save">
                  <Save className="h-4 w-4" /> {saving ? "Saving..." : "Save Config"}
                </Button>
                <Button onClick={test} variant="outline" disabled={testing || !config.host} className="gap-2" data-testid="mikrotik-test">
                  <TestTube2 className="h-4 w-4" /> {testing ? "Testing..." : "Test Connection"}
                </Button>
              </div>

              {/* Test Result */}
              {testResult && (
                <div className={`p-4 rounded-md border ${testResult.connected ? "border-emerald-500/30 bg-emerald-500/5" : "border-rose-500/30 bg-rose-500/5"}`}>
                  {testResult.connected ? (
                    <div className="space-y-1">
                      <div className="flex items-center gap-2 text-emerald-500 font-medium text-sm">
                        <CheckCircle2 className="h-4 w-4" /> Connected Successfully
                      </div>
                      <div className="text-xs text-muted-foreground space-y-0.5 font-mono">
                        <div>Router: {testResult.router_name}</div>
                        <div>Version: {testResult.version}</div>
                        <div>Uptime: {testResult.uptime}</div>
                        <div>CPU: {testResult.cpu_count} cores · RAM: {Math.round((testResult.total_memory || 0) / 1024 / 1024)}MB total, {Math.round((testResult.free_memory || 0) / 1024 / 1024)}MB free</div>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 text-rose-500 font-medium text-sm">
                      <XCircle className="h-4 w-4" /> Connection Failed: {testResult.error}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
