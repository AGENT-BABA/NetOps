import { useEffect, useState } from "react";
import { api, apiErrorMessage } from "@/lib/api";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { UserPlus, Wifi, WifiOff, Trash2 } from "lucide-react";
import { toast } from "sonner";

export default function AdminClients() {
  const [clients, setClients] = useState([]);
  const [dealers, setDealers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedClient, setSelectedClient] = useState(null);
  const [selectedDealer, setSelectedDealer] = useState("");
  const [busy, setBusy] = useState(false);

  // PPPoE assignment state
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignClient, setAssignClient] = useState(null);
  const [pppoeUsers, setPppoeUsers] = useState([]);
  const [selectedPppoe, setSelectedPppoe] = useState("");
  const [pppoeLoading, setPppoeLoading] = useState(false);

  // Unassign state
  const [unassignOpen, setUnassignOpen] = useState(false);
  const [unassignTarget, setUnassignTarget] = useState(null);

  const load = async () => {
    try {
      const [cRes, dRes] = await Promise.all([
        api.get("/admin/clients/all"),
        api.get("/admin/dealers"),
      ]);
      setClients(cRes.data);
      setDealers(dRes.data);
    } catch (e) {
      toast.error(apiErrorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  // Dealer assignment
  const doAssignDealer = async () => {
    if (!selectedClient || !selectedDealer) return;
    setBusy(true);
    try {
      await api.post(`/admin/clients/${selectedClient.id}/assign-dealer`, {
        dealer_id: selectedDealer,
      });
      toast.success(`${selectedClient.name} assigned to dealer`);
      setSelectedClient(null);
      setSelectedDealer("");
      load();
    } catch (e) {
      toast.error(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  // PPPoE assignment - fetch available users
  const openAssignRouter = async (client) => {
    setAssignClient(client);
    setAssignOpen(true);
    setPppoeLoading(true);
    setSelectedPppoe("");
    try {
      const { data } = await api.get("/admin/pppoe-users");
      setPppoeUsers(data.users || []);
    } catch (e) {
      toast.error(apiErrorMessage(e));
    } finally {
      setPppoeLoading(false);
    }
  };

  // Confirm PPPoE assignment
  const doAssignRouter = async () => {
    if (!assignClient || !selectedPppoe) return;
    setBusy(true);
    try {
      await api.post("/admin/assign-router/confirm", {
        client_id: assignClient.id,
        pppoe_username: selectedPppoe,
      });
      toast.success(`Router assigned to ${assignClient.name}`);
      setAssignOpen(false);
      setAssignClient(null);
      setSelectedPppoe("");
      load();
    } catch (e) {
      toast.error(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  // Unassign router
  const openUnassign = (client) => {
    setUnassignTarget(client);
    setUnassignOpen(true);
  };

  const doUnassign = async () => {
    if (!unassignTarget) return;
    setBusy(true);
    try {
      await api.delete(`/admin/unassign-router/${unassignTarget.router_id}`);
      toast.success(`Router removed from ${unassignTarget.name}`);
      setUnassignOpen(false);
      setUnassignTarget(null);
      load();
    } catch (e) {
      toast.error(apiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <div className="font-label text-[10px] text-muted-foreground">CLIENT MANAGEMENT</div>
        <h1 className="mt-1 font-display text-3xl font-semibold tracking-tight">Clients</h1>
        <p className="mt-1 text-sm text-muted-foreground">Assign clients to dealers and assign PPPoE routers.</p>
      </div>

      <Card className="border-border">
        <CardContent className="p-0 overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="font-label text-[10px]">NAME</TableHead>
                <TableHead className="font-label text-[10px]">EMAIL</TableHead>
                <TableHead className="font-label text-[10px]">PHONE</TableHead>
                <TableHead className="font-label text-[10px]">DEALER</TableHead>
                <TableHead className="font-label text-[10px]">ROUTER</TableHead>
                <TableHead className="font-label text-[10px] text-right">ACTIONS</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={i}><TableCell colSpan={6}><Skeleton className="h-8 w-full" /></TableCell></TableRow>
              ))}
              {!loading && clients.map((c) => (
                <TableRow key={c.id} className="row-hover" data-testid={`client-row-${c.email}`}>
                  <TableCell className="text-sm font-medium">{c.name}</TableCell>
                  <TableCell className="font-mono text-xs">{c.email}</TableCell>
                  <TableCell className="text-xs">{c.phone}</TableCell>
                  <TableCell className="text-xs">
                    {c.dealer_id ? (
                      <Badge variant="secondary" className="font-label text-[9px]">{c.dealer_code || "Unknown"}</Badge>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 gap-1 text-[10px]"
                        onClick={() => { setSelectedClient(c); }}
                        data-testid={`assign-dealer-btn-${c.email}`}
                      >
                        <UserPlus className="h-3 w-3" /> Assign Dealer
                      </Button>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.router_assigned ? (
                      <div className="flex items-center gap-1.5">
                        {c.router_status === "online" ? (
                          <Wifi className="h-3 w-3 text-emerald-500" />
                        ) : (
                          <WifiOff className="h-3 w-3 text-rose-500" />
                        )}
                        <span className="font-mono text-[10px]">{c.pppoe_username || c.router_id}</span>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-5 w-5 p-0 text-muted-foreground hover:text-rose-500"
                          onClick={() => openUnassign(c)}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 gap-1 text-[10px]"
                        onClick={() => openAssignRouter(c)}
                        data-testid={`assign-router-btn-${c.email}`}
                      >
                        <Wifi className="h-3 w-3" /> Assign Router
                      </Button>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {c.dealer_id && (
                      <Badge variant="outline" className="font-label text-[9px]">Active</Badge>
                    )}
                  </TableCell>
                </TableRow>
              ))}
              {!loading && clients.length === 0 && (
                <TableRow><TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">
                  No clients found.
                </TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* Dealer Assignment Dialog */}
      <Dialog open={!!selectedClient} onOpenChange={(o) => { if (!o) { setSelectedClient(null); setSelectedDealer(""); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Assign {selectedClient?.name} to Dealer</DialogTitle>
            <DialogDescription>
              Select a dealer to assign this client to. Their existing open tickets will also be reassigned.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5 py-2">
            <label className="font-label text-[10px] text-muted-foreground">SELECT DEALER</label>
            <Select value={selectedDealer} onValueChange={setSelectedDealer}>
              <SelectTrigger data-testid="dealer-select">
                <SelectValue placeholder="Choose a dealer…" />
              </SelectTrigger>
              <SelectContent>
                {dealers.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name} ({d.dealer_code})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setSelectedClient(null); setSelectedDealer(""); }}>Cancel</Button>
            <Button
              onClick={doAssignDealer}
              disabled={busy || !selectedDealer}
              data-testid="assign-confirm-btn"
            >
              {busy ? "Assigning…" : "Assign client"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* PPPoE Router Assignment Dialog */}
      <Dialog open={assignOpen} onOpenChange={(o) => { if (!o) { setAssignOpen(false); setAssignClient(null); setSelectedPppoe(""); } }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Assign Router to {assignClient?.name}</DialogTitle>
            <DialogDescription>
              Select an available PPPoE user from the MikroTik CCR to assign to this client.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5 py-2">
            <label className="font-label text-[10px] text-muted-foreground">PPPoE USERS</label>
            {pppoeLoading ? (
              <Skeleton className="h-10 w-full" />
            ) : (
              <Select value={selectedPppoe} onValueChange={setSelectedPppoe}>
                <SelectTrigger data-testid="pppoe-select">
                  <SelectValue placeholder="Choose a PPPoE user…" />
                </SelectTrigger>
                <SelectContent>
                  {pppoeUsers.filter((u) => !u.assigned).map((u) => (
                    <SelectItem key={u.id} value={u.name}>
                      {u.name} — {u.profile} {u.disabled ? "(disabled)" : ""}
                    </SelectItem>
                  ))}
                  {pppoeUsers.filter((u) => !u.assigned).length === 0 && (
                    <div className="px-3 py-2 text-sm text-muted-foreground">No available PPPoE users</div>
                  )}
                </SelectContent>
              </Select>
            )}
            {selectedPppoe && (
              <div className="mt-2 rounded-md border border-border bg-muted/50 p-3 text-xs">
                <div className="font-label text-[9px] text-muted-foreground">SELECTED</div>
                <div className="mt-1 font-mono">{selectedPppoe}</div>
                <div className="mt-1 text-muted-foreground">
                  Profile: {pppoeUsers.find((u) => u.name === selectedPppoe)?.profile}
                </div>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setAssignOpen(false); setAssignClient(null); setSelectedPppoe(""); }}>Cancel</Button>
            <Button
              onClick={doAssignRouter}
              disabled={busy || !selectedPppoe}
              data-testid="assign-router-confirm-btn"
            >
              {busy ? "Assigning…" : "Assign router"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Unassign Confirmation Dialog */}
      <Dialog open={unassignOpen} onOpenChange={(o) => { if (!o) { setUnassignOpen(false); setUnassignTarget(null); } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remove Router</DialogTitle>
            <DialogDescription>
              Are you sure you want to remove the router assignment from {unassignTarget?.name}? This will unlink PPPoE user <span className="font-mono">{unassignTarget?.pppoe_username}</span>.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setUnassignOpen(false); setUnassignTarget(null); }}>Cancel</Button>
            <Button
              variant="destructive"
              onClick={doUnassign}
              disabled={busy}
            >
              {busy ? "Removing…" : "Remove router"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
